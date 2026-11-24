"""Integration tests for JudgeBatchWorker and Database audit_status updates."""

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.core.config import get_settings
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import AuditStatus, ConstraintType, SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.models.extractions import (
    DiscourseSegment,
    ExtractedADR,
    ExtractedConstraint,
)
from checkcord.storage.meeting_extractions import MeetingExtractionStore
from checkcord.storage.postgres import PostgresEventStore, tenant_context
from checkcord.workers import judge_batch_worker as judge_batch_worker_module
from checkcord.workers.judge_batch_worker import JudgeBatchWorker, run_judge_batch_sweep

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_judge_batch_worker_run_once() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    store = MeetingExtractionStore()

    tenant_id = uuid4()
    parent_event_id = uuid4()

    segment = DiscourseSegment(
        parent_event_id=parent_event_id,
        tenant_id=tenant_id,
        text_content="Alice: We decided on Postgres.",
    )

    adr = ExtractedADR(
        decision_title="Adopt Postgres",
        context="Database choice",
        final_decision="Postgres",
        audit_status=AuditStatus.PENDING_AUDIT,
    )

    constraint = ExtractedConstraint(
        constraint_type=ConstraintType.TECHNICAL,
        description="Must run on Postgres",
        source_quote="Alice: We decided on Postgres.",
        audit_status=AuditStatus.PENDING_AUDIT,
    )

    # Clean up any leftover pending audit rows from earlier test runs
    async with engine.begin() as conn:
        for t in ["extracted_adrs", "extracted_constraints", "extracted_risks"]:
            await conn.execute(text(f"DELETE FROM {t} WHERE audit_status = 'pending_audit'"))

    # 1. Save segment & extractions as PENDING_AUDIT
    async with tenant_context(engine, tenant_id) as conn:
        await store.save_segment_with_extractions(conn, segment, [adr], [constraint], [])

    # 2. Mock OpenAI AsyncOpenAI client responses
    mock_openai = MagicMock()
    mock_file = MagicMock()
    mock_file.id = "file-test-123"
    mock_openai.files.create = AsyncMock(return_value=mock_file)

    mock_batch = MagicMock()
    mock_batch.id = "batch-test-123"
    mock_batch.status = "completed"
    mock_batch.output_file_id = "file-output-456"
    mock_openai.batches.create = AsyncMock(return_value=mock_batch)
    mock_openai.batches.retrieve = AsyncMock(return_value=mock_batch)

    # Output file content simulating 1 verified, 1 rejected
    line_verified = {
        "custom_id": f"judge-audit-{adr.id}",
        "response": {
            "body": {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "is_supported": True,
                                    "hallucinated_elements": [],
                                    "rejection_reason": None,
                                }
                            )
                        }
                    }
                ]
            }
        },
    }
    line_rejected = {
        "custom_id": f"judge-audit-{constraint.id}",
        "response": {
            "body": {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "is_supported": False,
                                    "hallucinated_elements": ["Must run on Postgres"],
                                    "rejection_reason": "Not explicitly stated as constraint",
                                }
                            )
                        }
                    }
                ]
            }
        },
    }
    output_content = json.dumps(line_verified) + "\n" + json.dumps(line_rejected)

    mock_content = MagicMock()
    mock_content.text = output_content
    mock_openai.files.content = AsyncMock(return_value=mock_content)

    # 3. Instantiate worker and run_once
    worker = JudgeBatchWorker(engine=engine, openai_client=mock_openai, batch_limit=10)
    processed_count = await worker.run_once(tenant_id=tenant_id)

    assert processed_count == 2

    # 4. Verify audit_statuses updated in DB under tenant context
    async with tenant_context(engine, tenant_id) as conn:
        res_adr = await conn.execute(
            text("SELECT audit_status FROM extracted_adrs WHERE id = :id"), {"id": adr.id}
        )
        assert res_adr.scalar() == "verified"

        res_con = await conn.execute(
            text("SELECT audit_status FROM extracted_constraints WHERE id = :id"),
            {"id": constraint.id},
        )
        assert res_con.scalar() == "rejected"

    await engine.dispose()


async def test_run_judge_batch_sweep_skips_without_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an OpenAI API key configured, the sweep must no-op rather than raise."""
    import checkcord.core.config as config_module
    from checkcord.core.config import Settings

    monkeypatch.setattr(config_module, "get_settings", lambda: Settings(openai_api_key=None))

    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        result = await run_judge_batch_sweep(engine)
        assert result == 0
    finally:
        await engine.dispose()


async def _register_tenants(engine: Any, *tenant_ids: Any) -> None:
    """Tenant sweeps only visit tenants that have a key or installation."""
    for tenant_id in tenant_ids:
        async with tenant_context(engine, tenant_id) as conn:
            await conn.execute(
                text(
                    "INSERT INTO api_keys (id, hashed_token, tenant_id, name, active, is_admin) "
                    "VALUES (:id, :h, :t, 'judge-test', true, false)"
                ),
                {"id": uuid4(), "h": uuid4().hex, "t": tenant_id},
            )


async def test_get_tenants_with_pending_extractions_finds_distinct_tenants() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    store = MeetingExtractionStore()

    tenant_a = uuid4()
    tenant_b = uuid4()
    tenant_c_no_pending = uuid4()
    await _register_tenants(engine, tenant_a, tenant_b, tenant_c_no_pending)

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        for t in ["extracted_adrs", "extracted_constraints", "extracted_risks"]:
            await conn.execute(text(f"DELETE FROM {t} WHERE audit_status = 'pending_audit'"))

    try:
        for tenant_id in (tenant_a, tenant_b):
            segment = DiscourseSegment(
                parent_event_id=uuid4(),
                tenant_id=tenant_id,
                text_content="Alice: We decided on Postgres.",
            )
            adr = ExtractedADR(
                decision_title="Adopt Postgres",
                context="Database choice",
                final_decision="Postgres",
                audit_status=AuditStatus.PENDING_AUDIT,
            )
            async with tenant_context(engine, tenant_id) as conn:
                await store.save_segment_with_extractions(conn, segment, [adr], [], [])

        # tenant_c_no_pending intentionally has no rows at all.
        pending_tenants = await store.get_tenants_with_pending_extractions(engine)

        assert tenant_a in pending_tenants
        assert tenant_b in pending_tenants
        assert tenant_c_no_pending not in pending_tenants
    finally:
        await engine.dispose()


async def test_run_judge_batch_sweep_processes_all_pending_tenants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    store = MeetingExtractionStore()

    tenant_a = uuid4()
    tenant_b = uuid4()
    await _register_tenants(engine, tenant_a, tenant_b)

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        for t in ["extracted_adrs", "extracted_constraints", "extracted_risks"]:
            await conn.execute(text(f"DELETE FROM {t} WHERE audit_status = 'pending_audit'"))

    adr_ids = {}
    try:
        for tenant_id in (tenant_a, tenant_b):
            segment = DiscourseSegment(
                parent_event_id=uuid4(),
                tenant_id=tenant_id,
                text_content="Alice: We decided on Postgres.",
            )
            adr = ExtractedADR(
                decision_title="Adopt Postgres",
                context="Database choice",
                final_decision="Postgres",
                audit_status=AuditStatus.PENDING_AUDIT,
            )
            adr_ids[tenant_id] = adr.id
            async with tenant_context(engine, tenant_id) as conn:
                await store.save_segment_with_extractions(conn, segment, [adr], [], [])

        # Mock AsyncOpenAI so JudgeBatchWorker() constructed internally by the sweep
        # doesn't require a real API key or network access.
        mock_openai = MagicMock()
        mock_file = MagicMock()
        mock_file.id = "file-test-123"
        mock_openai.files.create = AsyncMock(return_value=mock_file)

        mock_batch = MagicMock()
        mock_batch.id = "batch-test-123"
        mock_batch.status = "completed"
        mock_batch.output_file_id = "file-output-456"
        mock_openai.batches.create = AsyncMock(return_value=mock_batch)
        mock_openai.batches.retrieve = AsyncMock(return_value=mock_batch)

        def _make_output(adr_id: object) -> str:
            line = {
                "custom_id": f"judge-audit-{adr_id}",
                "response": {
                    "body": {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "is_supported": True,
                                            "hallucinated_elements": [],
                                            "rejection_reason": None,
                                        }
                                    )
                                }
                            }
                        ]
                    }
                },
            }
            return json.dumps(line)

        async def _fake_content(_file_id: str) -> MagicMock:
            # Each call returns output for whichever tenant is currently being processed;
            # since both tenants have one ADR each, respond generically for either id.
            mock_content = MagicMock()
            mock_content.text = "\n".join(_make_output(adr_id) for adr_id in adr_ids.values())
            return mock_content

        mock_openai.files.content = AsyncMock(side_effect=_fake_content)

        import checkcord.core.config as config_module
        from checkcord.core.config import Settings

        patched_settings = Settings(
            openai_api_key="sk-test-dummy", database_url=settings.database_url
        )
        monkeypatch.setattr(config_module, "get_settings", lambda: patched_settings)
        monkeypatch.setattr(
            judge_batch_worker_module, "build_openai_client", lambda *a, **kw: mock_openai
        )

        total = await run_judge_batch_sweep(engine)
        assert total >= 2

        for tenant_id, adr_id in adr_ids.items():
            async with tenant_context(engine, tenant_id) as conn:
                res = await conn.execute(
                    text("SELECT audit_status FROM extracted_adrs WHERE id = :id"), {"id": adr_id}
                )
                assert res.scalar() == "verified"
    finally:
        await engine.dispose()


@pytest.mark.parametrize("audience_from", ["segment", "event", "none"])
async def test_judge_batch_worker_publishes_verified_extraction_onto_ingest_pipeline(
    audience_from: str,
) -> None:
    """A verified extraction is republished with the meeting's author, audience
    and connection. They come from the segment; older segments fall back to a
    stored meeting event; with neither, nothing is published.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    store = MeetingExtractionStore()

    tenant_id = uuid4()
    author = AuthorInfo(source_id="host_1", email="alice@acme.example")
    access_control = AccessControl(is_public=False, allowed_identity_ids=[uuid4()])

    meeting_event = CanonicalEvent(
        tenant_id=tenant_id,
        source_platform=SourcePlatform.ZOOM,
        source_type=SourceType.MEETING_TRANSCRIPT,
        source_id="zoom_mtg_publish_test",
        happened_at=datetime.now(UTC),
        author=author,
        content_raw="Alice: We decided on Postgres.",
        content_clean="Alice: We decided on Postgres.",
        access_control=access_control,
        connection="zoom:acct-1",
    )

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        for t in ["extracted_adrs", "extracted_constraints", "extracted_risks"]:
            await conn.execute(text(f"DELETE FROM {t} WHERE audit_status = 'pending_audit'"))

    adr = ExtractedADR(
        decision_title="Adopt Postgres",
        context="Database choice",
        final_decision="Postgres",
        audit_status=AuditStatus.PENDING_AUDIT,
    )
    segment = DiscourseSegment(
        parent_event_id=meeting_event.event_id,
        tenant_id=tenant_id,
        text_content="Alice: We decided on Postgres.",
    )

    try:
        async with tenant_context(engine, tenant_id) as conn:
            pg_store = PostgresEventStore(conn)
            if audience_from == "event":
                await pg_store.save_event(meeting_event)
            on_segment = audience_from == "segment"
            await store.save_segment_with_extractions(
                conn,
                segment,
                [adr],
                [],
                [],
                connection="zoom:acct-1",
                author=author if on_segment else None,
                access_control=access_control if on_segment else None,
            )

        mock_openai = MagicMock()
        mock_file = MagicMock()
        mock_file.id = "file-test-publish"
        mock_openai.files.create = AsyncMock(return_value=mock_file)

        mock_batch = MagicMock()
        mock_batch.id = "batch-test-publish"
        mock_batch.status = "completed"
        mock_batch.output_file_id = "file-output-publish"
        mock_openai.batches.create = AsyncMock(return_value=mock_batch)
        mock_openai.batches.retrieve = AsyncMock(return_value=mock_batch)

        line_verified = {
            "custom_id": f"judge-audit-{adr.id}",
            "response": {
                "body": {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "is_supported": True,
                                        "hallucinated_elements": [],
                                        "rejection_reason": None,
                                    }
                                )
                            }
                        }
                    ]
                }
            },
        }
        mock_content = MagicMock()
        mock_content.text = json.dumps(line_verified)
        mock_openai.files.content = AsyncMock(return_value=mock_content)

        mock_bus = AsyncMock()
        worker = JudgeBatchWorker(
            engine=engine, openai_client=mock_openai, batch_limit=10, bus=mock_bus
        )
        await worker.run_once(tenant_id=tenant_id)

        if audience_from == "none":
            mock_bus.publish.assert_not_called()
            return
        mock_bus.publish.assert_called_once()
        topic, published_event = mock_bus.publish.call_args.args
        assert topic == "events.raw.custom"
        assert published_event.tenant_id == tenant_id
        assert published_event.metadata["context_type"] == "adr"
        assert published_event.author == author
        assert published_event.access_control == access_control
        assert published_event.connection == "zoom:acct-1"
        assert published_event.content_clean == "Postgres"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM extracted_adrs WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM meeting_discourse_segments WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
            await conn.execute(
                text("DELETE FROM events WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_judge_batch_worker_skips_publish_when_bus_not_configured() -> None:
    """Omitting bus must not raise; verification and audit_status updates
    still run.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    store = MeetingExtractionStore()
    tenant_id = uuid4()

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        for t in ["extracted_adrs", "extracted_constraints", "extracted_risks"]:
            await conn.execute(text(f"DELETE FROM {t} WHERE audit_status = 'pending_audit'"))

    adr = ExtractedADR(
        decision_title="No bus configured",
        context="Should not raise",
        final_decision="Postgres",
        audit_status=AuditStatus.PENDING_AUDIT,
    )
    segment = DiscourseSegment(
        parent_event_id=uuid4(),
        tenant_id=tenant_id,
        text_content="Alice: We decided on Postgres.",
    )

    try:
        async with tenant_context(engine, tenant_id) as conn:
            await store.save_segment_with_extractions(conn, segment, [adr], [], [])

        mock_openai = MagicMock()
        mock_file = MagicMock()
        mock_file.id = "file-test-nobus"
        mock_openai.files.create = AsyncMock(return_value=mock_file)
        mock_batch = MagicMock()
        mock_batch.id = "batch-test-nobus"
        mock_batch.status = "completed"
        mock_batch.output_file_id = "file-output-nobus"
        mock_openai.batches.create = AsyncMock(return_value=mock_batch)
        mock_openai.batches.retrieve = AsyncMock(return_value=mock_batch)
        line_verified = {
            "custom_id": f"judge-audit-{adr.id}",
            "response": {
                "body": {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "is_supported": True,
                                        "hallucinated_elements": [],
                                        "rejection_reason": None,
                                    }
                                )
                            }
                        }
                    ]
                }
            },
        }
        mock_content = MagicMock()
        mock_content.text = json.dumps(line_verified)
        mock_openai.files.content = AsyncMock(return_value=mock_content)

        worker = JudgeBatchWorker(engine=engine, openai_client=mock_openai, batch_limit=10)
        processed_count = await worker.run_once(tenant_id=tenant_id)
        assert processed_count == 1

        async with tenant_context(engine, tenant_id) as conn:
            res = await conn.execute(
                text("SELECT audit_status FROM extracted_adrs WHERE id = :id"), {"id": adr.id}
            )
            assert res.scalar() == "verified"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM extracted_adrs WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM meeting_discourse_segments WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
        await engine.dispose()
