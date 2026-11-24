import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core.config import get_settings
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import ConstraintType, RiskLevel, SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.models.extractions import (
    ExtractedADR,
    ExtractedConstraint,
    ExtractedRisk,
    VerifiedExtraction,
)
from checkcord.pipeline.agents.preprocessor import LLMSegment, SegmentationOutput
from checkcord.pipeline.agents.specialists import ADROutput, ConstraintOutput, RiskOutput
from checkcord.storage.postgres import tenant_context
from checkcord.workers.transcript_worker import TranscriptExtractionWorker

pytestmark = pytest.mark.integration


MEETING_TEXT = (
    "Alice: We decided on Postgres. Bob: GDPR mandates user deletion. "
    "DB migration downtime is a risk."
)


def make_meeting_event(content: str) -> CanonicalEvent:
    return CanonicalEvent(
        tenant_id=uuid4(),
        source_platform=SourcePlatform.ZOOM,
        source_type=SourceType.MEETING_TRANSCRIPT,
        source_id="zoom_mtg_999",
        happened_at=datetime.now(UTC),
        author=AuthorInfo(source_id="host_1"),
        content_raw=content,
        content_clean=content,
        access_control=AccessControl(is_public=True),
    )


def make_chat_event() -> CanonicalEvent:
    return CanonicalEvent(
        tenant_id=uuid4(),
        source_platform=SourcePlatform.SLACK,
        source_type=SourceType.MESSAGE,
        source_id="slack_msg_111",
        happened_at=datetime.now(UTC),
        author=AuthorInfo(source_id="user_1"),
        content_raw="Hey there",
        content_clean="Hey there",
        access_control=AccessControl(is_public=True),
    )


@pytest.mark.asyncio
async def test_process_message_skips_non_meeting() -> None:
    bus_mock = MagicMock()
    worker = TranscriptExtractionWorker(bus=bus_mock)

    chat_event = make_chat_event()
    await worker.process_message(chat_event)

    assert len(worker.processed_segments) == 0
    assert len(worker.extracted_adrs) == 0


@pytest.mark.asyncio
async def test_process_message_meeting_full_pipeline_success() -> None:
    bus_mock = MagicMock()
    bus_mock.publish = AsyncMock()

    mock_llm_client = MagicMock()
    mock_llm_client.judge_settled = AsyncMock(return_value="SETTLED")

    def mock_with_structured(output_schema):
        mock_call = AsyncMock()
        if output_schema == SegmentationOutput:
            mock_call.ainvoke.return_value = SegmentationOutput(
                segments=[
                    LLMSegment(
                        start_time=0.0,
                        end_time=120.0,
                        topics=["architecture", "security"],
                        speakers=["Alice", "Bob"],
                        text_content=MEETING_TEXT,
                    )
                ]
            )
        elif output_schema == ADROutput:
            mock_call.ainvoke.return_value = ADROutput(
                items=[
                    ExtractedADR(
                        decision_title="Use Postgres",
                        context="Multi-tenant isolation",
                        alternatives_considered=["MongoDB"],
                        final_decision="Postgres",
                        primary_driver="Alice",
                    )
                ]
            )
        elif output_schema == ConstraintOutput:
            mock_call.ainvoke.return_value = ConstraintOutput(
                items=[
                    ExtractedConstraint(
                        constraint_type=ConstraintType.REGULATORY,
                        description="GDPR deletion mandate",
                        source_quote="GDPR mandates user deletion.",
                    )
                ]
            )
        elif output_schema == RiskOutput:
            mock_call.ainvoke.return_value = RiskOutput(
                items=[
                    ExtractedRisk(
                        risk_level=RiskLevel.MED,
                        description="DB migration downtime risk",
                        potential_mitigation="Zero-downtime migrations",
                    )
                ]
            )
        elif output_schema == VerifiedExtraction:
            mock_call.ainvoke.return_value = VerifiedExtraction(
                is_supported=True,
            )
        return mock_call

    mock_llm_client.with_structured_output.side_effect = mock_with_structured

    worker = TranscriptExtractionWorker(bus=bus_mock, llm_client=mock_llm_client, sync_judge=True)
    event = make_meeting_event(MEETING_TEXT)

    await worker.process_message(event)

    assert len(worker.processed_segments) == 1
    assert len(worker.extracted_adrs) == 1
    assert len(worker.extracted_constraints) == 1
    assert len(worker.extracted_risks) == 1
    assert worker.rejected_extractions_count == 0

    # Each verified extraction is republished onto events.raw.custom with
    # the right context_type, so it goes through the usual ingest and dedup
    # path and is not written to canonical_contexts directly.
    assert bus_mock.publish.call_count == 3
    published_topics = {call.args[0] for call in bus_mock.publish.call_args_list}
    assert published_topics == {"events.raw.custom"}
    published_context_types = {
        call.args[1].metadata["context_type"] for call in bus_mock.publish.call_args_list
    }
    assert published_context_types == {"adr", "constraint", "risk"}
    for call in bus_mock.publish.call_args_list:
        published_event = call.args[1]
        assert published_event.tenant_id == event.tenant_id
        assert published_event.author == event.author
        assert published_event.access_control == event.access_control


@pytest.mark.asyncio
async def test_process_message_judge_rejection() -> None:
    bus_mock = MagicMock()
    bus_mock.publish = AsyncMock()

    mock_llm_client = MagicMock()
    mock_llm_client.judge_settled = AsyncMock(return_value="SETTLED")

    def mock_with_structured(output_schema):
        mock_call = AsyncMock()
        if output_schema == SegmentationOutput:
            mock_call.ainvoke.return_value = SegmentationOutput(
                segments=[
                    LLMSegment(
                        start_time=0.0,
                        end_time=60.0,
                        topics=["tech_debt"],
                        speakers=["Bob"],
                        text_content="Bob: Vague text.",
                    )
                ]
            )
        elif output_schema == ADROutput:
            mock_call.ainvoke.return_value = ADROutput(
                items=[
                    ExtractedADR(
                        decision_title="Hallucinated Decision",
                        context="Fake",
                        final_decision="Fake",
                    )
                ]
            )
        elif output_schema == ConstraintOutput:
            mock_call.ainvoke.return_value = ConstraintOutput(items=[])
        elif output_schema == RiskOutput:
            mock_call.ainvoke.return_value = RiskOutput(items=[])
        elif output_schema == VerifiedExtraction:
            # Judge rejects the hallucinated decision
            mock_call.ainvoke.return_value = VerifiedExtraction(
                is_supported=False,
                hallucinated_elements=["Hallucinated Decision"],
                rejection_reason="Not present in source text.",
            )
        return mock_call

    mock_llm_client.with_structured_output.side_effect = mock_with_structured

    worker = TranscriptExtractionWorker(bus=bus_mock, llm_client=mock_llm_client, sync_judge=True)
    event = make_meeting_event("Bob: Vague text.")

    await worker.process_message(event)

    assert len(worker.processed_segments) == 1
    assert len(worker.extracted_adrs) == 0  # Rejected by Judge!
    assert worker.rejected_extractions_count == 1
    # Nothing verified -> nothing republished.
    bus_mock.publish.assert_not_called()


@pytest.mark.asyncio
async def test_worker_e2e_redis_stream(async_redis_client: Redis) -> None:
    bus = RedisStreamsEventBus(async_redis_client)
    topic = "events.raw.meetings"
    group = "meeting_extractors"

    mock_llm_client = MagicMock()
    mock_llm_client.judge_settled = AsyncMock(return_value="SETTLED")

    def mock_with_structured(output_schema):
        mock_call = AsyncMock()
        if output_schema == SegmentationOutput:
            mock_call.ainvoke.return_value = SegmentationOutput(
                segments=[
                    LLMSegment(
                        start_time=0.0,
                        end_time=60.0,
                        topics=["security"],
                        speakers=["Bob"],
                        text_content="Bob: Security discussion. The unencrypted payload is a risk.",
                    )
                ]
            )
        elif output_schema == ADROutput:
            mock_call.ainvoke.return_value = ADROutput(items=[])
        elif output_schema == ConstraintOutput:
            mock_call.ainvoke.return_value = ConstraintOutput(items=[])
        elif output_schema == RiskOutput:
            mock_call.ainvoke.return_value = RiskOutput(
                items=[
                    ExtractedRisk(
                        risk_level=RiskLevel.HIGH,
                        description="Unencrypted payload risk",
                    )
                ]
            )
        elif output_schema == VerifiedExtraction:
            mock_call.ainvoke.return_value = VerifiedExtraction(
                is_supported=True,
            )
        return mock_call

    mock_llm_client.with_structured_output.side_effect = mock_with_structured

    worker = TranscriptExtractionWorker(
        bus=bus, group_name=group, consumer_name="test-worker", llm_client=mock_llm_client
    )

    await bus._ensure_group(topic, group)
    meeting_event = make_meeting_event(
        "Bob: Security discussion. The unencrypted payload is a risk."
    )
    await bus.publish(topic, meeting_event)

    worker_task = asyncio.create_task(worker.start([topic]))

    # Poll actual completion state instead of a fixed sleep: the shared process-wide
    # rate limiter accumulates throttling debt across the pytest session, so a fixed
    # delay is flaky when this test runs alongside the rest of the suite.
    for _ in range(100):
        if worker.extracted_risks:
            break
        await asyncio.sleep(0.1)
    worker.stop()
    await worker_task

    assert len(worker.processed_segments) == 1
    assert len(worker.extracted_risks) == 1
    assert worker.extracted_risks[0].risk_level == RiskLevel.HIGH


@pytest.mark.asyncio
async def test_worker_e2e_redis_stream_persists_extractions_to_postgres(
    async_redis_client: Redis,
) -> None:
    """Full wiring test: a synthetic transcript published to the real Redis stream this worker
    is registered on in `checkcord/worker.py` (`events.raw.meetings`) must end up as
    real rows in `extracted_risks` (and would for ADRs/Constraints given matching content).
    """
    bus = RedisStreamsEventBus(async_redis_client)
    topic = "events.raw.meetings"
    group = "meeting_extractors_pg_test"
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    mock_llm_client = MagicMock()
    mock_llm_client.judge_settled = AsyncMock(return_value="SETTLED")

    def mock_with_structured(output_schema):
        mock_call = AsyncMock()
        if output_schema == SegmentationOutput:
            mock_call.ainvoke.return_value = SegmentationOutput(
                segments=[
                    LLMSegment(
                        start_time=0.0,
                        end_time=60.0,
                        topics=["security"],
                        speakers=["Bob"],
                        text_content="Bob: Security discussion. The unencrypted payload is a risk.",
                    )
                ]
            )
        elif output_schema == ADROutput:
            mock_call.ainvoke.return_value = ADROutput(items=[])
        elif output_schema == ConstraintOutput:
            mock_call.ainvoke.return_value = ConstraintOutput(items=[])
        elif output_schema == RiskOutput:
            mock_call.ainvoke.return_value = RiskOutput(
                items=[
                    ExtractedRisk(
                        risk_level=RiskLevel.HIGH,
                        description="Unencrypted payload risk",
                    )
                ]
            )
        elif output_schema == VerifiedExtraction:
            mock_call.ainvoke.return_value = VerifiedExtraction(is_supported=True)
        return mock_call

    mock_llm_client.with_structured_output.side_effect = mock_with_structured

    worker = TranscriptExtractionWorker(
        bus=bus,
        engine=engine,
        group_name=group,
        consumer_name="test-worker-pg",
        llm_client=mock_llm_client,
        sync_judge=True,
    )

    meeting_event = make_meeting_event(
        "Bob: Security discussion. The unencrypted payload is a risk."
    )
    tenant_id = meeting_event.tenant_id

    try:
        await bus._ensure_group(topic, group)
        await bus.publish(topic, meeting_event)

        worker_task = asyncio.create_task(worker.start([topic]))

        # Poll the database directly rather than a fixed sleep or the worker's in-memory
        # list: this test's sync_judge=True path makes extra global_rate_limiter.acquire()
        # calls on top of extraction (that limiter is a process-wide singleton shared with
        # every other test in this session, so its throttling delay is not constant), and
        # `worker.extracted_risks` is populated *before* the DB write it's followed by --
        # stopping the worker as soon as that in-memory list is non-empty can cancel the
        # still-in-flight `save_segment_with_extractions` call, so the DB row must be the
        # actual condition polled on.
        rows: list[Any] = []
        for _ in range(100):
            async with tenant_context(engine, tenant_id) as conn:
                res = await conn.execute(
                    text(
                        "SELECT description, risk_level FROM extracted_risks "
                        "WHERE tenant_id = :tenant_id"
                    ),
                    {"tenant_id": tenant_id},
                )
                rows = res.fetchall()
            if rows:
                break
            await asyncio.sleep(0.1)

        worker.stop()
        await worker_task

        assert len(worker.extracted_risks) == 1
        assert len(rows) == 1
        assert rows[0].description == "Unencrypted payload risk"
        assert rows[0].risk_level == "high"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM extracted_risks WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
            await conn.execute(
                text("DELETE FROM meeting_discourse_segments WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
        await engine.dispose()


def test_meetings_are_judged_directly_by_default() -> None:
    assert TranscriptExtractionWorker(bus=MagicMock()).sync_judge is True


@pytest.mark.asyncio
async def test_an_extraction_the_segment_does_not_support_is_dropped_before_judging() -> None:
    bus_mock = MagicMock()
    bus_mock.publish = AsyncMock()
    segment_text = "Tomas: we decided ledger.post_entry is the only writer to the ledger table."
    judged: list[Any] = []
    llm = MagicMock()
    llm.judge_settled = AsyncMock(return_value="SETTLED")

    def structured(output_schema: Any) -> Any:
        call = AsyncMock()
        if output_schema == SegmentationOutput:
            call.ainvoke.return_value = SegmentationOutput(
                segments=[
                    LLMSegment(
                        start_time=0.0,
                        end_time=60.0,
                        topics=["ledger"],
                        speakers=["Tomas"],
                        text_content=segment_text,
                    )
                ]
            )
        elif output_schema == ADROutput:
            call.ainvoke.return_value = ADROutput(
                items=[
                    ExtractedADR(
                        decision_title="Ledger writer",
                        context="Duplicate rows",
                        final_decision="ledger.post_entry is the only writer to the ledger table",
                    ),
                    ExtractedADR(
                        decision_title="Retention",
                        context="Audit",
                        final_decision="ledger rows are kept for 7 years in cold storage",
                    ),
                ]
            )
        elif output_schema == VerifiedExtraction:

            async def verdict(*args: Any, **kwargs: Any) -> VerifiedExtraction:
                judged.append(args)
                return VerifiedExtraction(is_supported=True)

            call.ainvoke.side_effect = verdict
        else:
            call.ainvoke.return_value = output_schema(items=[])
        return call

    llm.with_structured_output.side_effect = structured
    worker = TranscriptExtractionWorker(bus=bus_mock, llm_client=llm, sync_judge=True)

    await worker.process_message(make_meeting_event(segment_text))

    assert [a.final_decision for a in worker.extracted_adrs] == [
        "ledger.post_entry is the only writer to the ledger table"
    ]
    assert len(judged) == 1
    assert worker.rejected_extractions_count == 1


@pytest.mark.asyncio
async def test_the_settled_check_uses_the_judge_it_is_given() -> None:
    judge = MagicMock()
    judge.judge_settled = AsyncMock(return_value="OPEN")
    worker = TranscriptExtractionWorker(bus=MagicMock(), settled_judge=judge)
    assert await worker._settled("Bob: maybe Postgres?", "Use Postgres") is False
    judge.judge_settled.assert_awaited_once_with("Bob: maybe Postgres?", "Use Postgres")
