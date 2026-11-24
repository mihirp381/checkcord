"""Batch-judged meeting extractions keep the meeting's audience through to the stored record."""

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app_role_helpers import app_database_url, create_app_role, drop_app_role
from fastapi.testclient import TestClient
from key_helpers import KeySpec, seeded_keys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.core.config import get_settings
from checkcord.main import app
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import AccessVisibility, AclType, AuditStatus
from checkcord.models.extractions import DiscourseSegment, ExtractedADR
from checkcord.pipeline.clustering import ClusteringWorker
from checkcord.pipeline.consumer import PipelineConsumer
from checkcord.pipeline.enricher import PipelineEnricher
from checkcord.pipeline.linkage import LinkageWorker
from checkcord.storage.meeting_extractions import MeetingExtractionStore
from checkcord.storage.postgres import create_checkcord_engine, tenant_context
from checkcord.workers.judge_batch_worker import JudgeBatchWorker

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ADMIN_URL = get_settings().database_url


class CaptureBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    async def publish(self, topic: str, event: Any) -> str:
        self.published.append((topic, event))
        return "0-0"

    def take(self, topic: str) -> list[Any]:
        taken = [e for t, e in self.published if t == topic]
        self.published = [(t, e) for t, e in self.published if t != topic]
        return taken


class FixedEmbedder:
    async def embed_text(self, text: str) -> list[float]:
        return [0.0, 1.0] + [0.0] * 1022


class Linker:
    async def judge_linkage(self, summary: str, content: str) -> str:
        return "same"


@dataclass
class World:
    tenant_id: UUID
    engine: AsyncEngine
    attendee: UUID


async def _admin(sql: str, params: dict[str, Any]) -> list[Any]:
    engine = create_async_engine(ADMIN_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            result = await conn.execute(text(sql), params)
            return list(result.fetchall()) if result.returns_rows else []
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[World, None]:
    tenant_id = uuid4()
    specs = [
        KeySpec("batch_judge_attendee", tenant_id, user_email=f"a-{tenant_id}@acme.example"),
        KeySpec("batch_judge_other", tenant_id, user_email=f"o-{tenant_id}@acme.example"),
    ]
    async with seeded_keys(specs) as users:
        role, url = await create_app_role()
        engine = create_checkcord_engine(url)
        try:
            with app_database_url(url):
                monkeypatch.setattr(get_settings(), "redaction_model_pass", False)
                yield World(tenant_id, engine, users[f"a-{tenant_id}@acme.example"])
        finally:
            await engine.dispose()
            await drop_app_role(role)
            for table in (
                "context_event_links",
                "canonical_contexts",
                "events",
                "extracted_adrs",
                "meeting_discourse_segments",
            ):
                await _admin(f"DELETE FROM {table} WHERE tenant_id = :t", {"t": tenant_id})


def _openai(adr_id: UUID) -> MagicMock:
    client = MagicMock()
    client.files.create = AsyncMock(return_value=MagicMock(id="file-in"))
    batch = MagicMock(id="batch-1", status="completed", output_file_id="file-out")
    client.batches.create = AsyncMock(return_value=batch)
    client.batches.retrieve = AsyncMock(return_value=batch)
    verdict = {"is_supported": True, "hallucinated_elements": [], "rejection_reason": None}
    line = {
        "custom_id": f"judge-audit-{adr_id}",
        "response": {"body": {"choices": [{"message": {"content": json.dumps(verdict)}}]}},
    }
    client.files.content = AsyncMock(return_value=MagicMock(text=json.dumps(line)))
    return client


async def _store(w: World, bus: CaptureBus) -> UUID:
    [event] = bus.take("events.raw.custom")
    enricher = PipelineEnricher(bus=bus, engine=w.engine)
    consumer = PipelineConsumer(bus, w.engine, embedder=FixedEmbedder())
    clustering = ClusteringWorker(bus=bus, engine=w.engine)
    linkage = LinkageWorker(bus=bus, engine=w.engine, llm_client=Linker())
    await enricher.process_message(event)
    [enriched] = bus.take("events.enriched.chat")
    await consumer.process_message(enriched)
    [pending] = bus.take("events.clustering.pending")
    await clustering.process_message(pending)
    [linking] = bus.take("events.linkage.pending")
    await linkage.process_message(linking)
    [row] = await _admin(
        "SELECT DISTINCT canonical_id FROM context_event_links WHERE tenant_id = :t",
        {"t": w.tenant_id},
    )
    record_id: UUID = row[0]
    return record_id


def _status(token: str, record_id: UUID) -> int:
    with TestClient(app) as client:
        res = client.get(
            f"/api/v1/graph/node/context:{record_id}?repo_identifier=acme/any",
            headers={"Authorization": f"Bearer {token}"},
        )
    return res.status_code


async def test_batch_judged_extraction_is_readable_by_attendees_only(world: World) -> None:
    audience = AccessControl(
        is_public=False,
        visibility=AccessVisibility.PRIVATE,
        acl_type=AclType.CUSTOM,
        allowed_identity_ids=[world.attendee],
    )
    host = AuthorInfo(source_id="host-1", display_name="Host")
    adr = ExtractedADR(
        decision_title="Adopt idempotent charge retries",
        context="Payments",
        final_decision="Retry with an idempotency key",
        audit_status=AuditStatus.PENDING_AUDIT,
    )
    segment = DiscourseSegment(
        parent_event_id=uuid4(),
        tenant_id=world.tenant_id,
        text_content="Host: we retry charges with an idempotency key.",
    )
    async with tenant_context(world.engine, world.tenant_id) as conn:
        await MeetingExtractionStore().save_segment_with_extractions(
            conn,
            segment,
            [adr],
            [],
            [],
            connection="zoom:acct-9",
            author=host,
            access_control=audience,
        )

    bus = CaptureBus()
    worker = JudgeBatchWorker(
        engine=world.engine, openai_client=_openai(adr.id), batch_limit=10, bus=bus
    )
    assert await worker.run_once(tenant_id=world.tenant_id) == 1
    [published] = [e for t, e in bus.published if t == "events.raw.custom"]
    assert published.author == host and published.access_control == audience
    assert published.connection == "zoom:acct-9"

    record_id = await _store(world, bus)
    [(stored, author_id)] = await _admin(
        "SELECT c.access_control, e.author->>'source_id' FROM canonical_contexts c "
        "JOIN context_event_links l ON l.canonical_id = c.id "
        "JOIN events e ON e.event_id = l.event_id WHERE c.id = :id",
        {"id": record_id},
    )
    assert stored["is_public"] is False
    assert stored["allowed_identity_ids"] == [str(world.attendee)]
    assert author_id == "host-1"

    assert _status("batch_judge_attendee", record_id) == 200
    assert _status("batch_judge_other", record_id) == 404
