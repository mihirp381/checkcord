"""Tests for the Pipeline Consumer."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.pipeline.consumer import PipelineConsumer
from checkcord.storage import event_pipeline
from checkcord.storage.event_pipeline import ClaimedEvent


@pytest.fixture(autouse=True)
def pipeline_rows() -> Iterator[MagicMock]:
    """Stub the row-progress calls; by default the claim finds nothing left to do."""
    with patch("checkcord.pipeline.consumer.event_pipeline") as rows:
        rows.claim = AsyncMock(return_value=None)
        rows.mark_done = AsyncMock()
        rows.mark_attempt_failed = AsyncMock(return_value=event_pipeline.PENDING)
        rows.describe_error = event_pipeline.describe_error
        rows.FAILED = event_pipeline.FAILED
        yield rows


@pytest.fixture
def mock_bus() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_engine() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def dummy_event() -> Any:
    """Mock a CanonicalEvent."""
    event = AsyncMock()
    event.tenant_id = uuid4()
    event.event_id = uuid4()
    event.metadata = {}
    return event


@pytest.mark.asyncio
async def test_consumer_shutdown_gracefully(mock_bus: AsyncMock, mock_engine: AsyncMock) -> None:
    """Ensure stop() breaks the loop and cancels subscriptions safely."""
    consumer = PipelineConsumer(mock_bus, mock_engine)

    # We want subscribe to block indefinitely like a real redis loop
    async def infinite_subscribe(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(10)

    mock_bus.subscribe.side_effect = infinite_subscribe

    # Start consumer in background
    task = asyncio.create_task(consumer.start(["topic.test"]))

    # Give it a moment to start
    await asyncio.sleep(0.05)
    assert not task.done()

    # Trigger shutdown
    consumer.stop()

    # Wait for task to finish
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()

    mock_bus.subscribe.assert_called_once()


@pytest.mark.asyncio
@patch("checkcord.pipeline.consumer.tenant_context")
@patch("checkcord.pipeline.consumer.PostgresEventStore")
async def test_process_message_success(
    mock_store_cls: AsyncMock,
    mock_tenant_context: AsyncMock,
    mock_bus: AsyncMock,
    mock_engine: AsyncMock,
    dummy_event: Any,
) -> None:
    """Verify tenant isolation context is opened dynamically."""

    # Setup context manager mock
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_tenant_context.return_value = mock_context

    # Setup store mock
    mock_store_instance = AsyncMock()
    mock_store_instance.save_event.return_value = True
    mock_store_cls.return_value = mock_store_instance

    consumer = PipelineConsumer(mock_bus, mock_engine)
    await consumer.process_message(dummy_event)

    # Assert context was opened with correct tenant_id
    assert mock_tenant_context.call_args_list[0].args == (mock_engine, dummy_event.tenant_id)

    # Assert store was created with the isolated connection and save_event called
    mock_store_cls.assert_called_once_with(mock_conn)
    mock_store_instance.save_event.assert_called_once_with(dummy_event, pipeline_pending=True)


@pytest.mark.asyncio
@patch("checkcord.pipeline.consumer.tenant_context")
@patch("checkcord.pipeline.consumer.PostgresEventStore")
async def test_process_message_exception_bubbling(
    mock_store_cls: AsyncMock,
    mock_tenant_context: AsyncMock,
    mock_bus: AsyncMock,
    mock_engine: AsyncMock,
    dummy_event: Any,
) -> None:
    """Verify exceptions bubble up so the EventBus can handle DLQ routing."""

    # Setup context manager mock
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_tenant_context.return_value = mock_context

    # Setup store mock to fail
    mock_store_instance = AsyncMock()
    mock_store_instance.save_event.side_effect = RuntimeError("DB down")
    mock_store_cls.return_value = mock_store_instance

    consumer = PipelineConsumer(mock_bus, mock_engine)

    with pytest.raises(RuntimeError, match="DB down"):
        await consumer.process_message(dummy_event)


def make_canonical_event(source_type: SourceType = SourceType.MESSAGE) -> CanonicalEvent:
    """Helper to construct a valid CanonicalEvent."""
    return CanonicalEvent(
        tenant_id=uuid4(),
        source_platform=SourcePlatform.SLACK,
        source_type=source_type,
        source_id="msg_123",
        happened_at=datetime.now(UTC),
        author={"source_id": "user_456", "display_name": "Test User"},
        content_raw="hello we decided to do X",
        content_clean="hello we decided to do X",
        access_control={"is_public": False},
    )


@pytest.mark.asyncio
@patch("checkcord.pipeline.consumer.tenant_context")
@patch("checkcord.pipeline.consumer.PostgresEventStore")
async def test_process_message_with_embedder(
    mock_store_cls: AsyncMock,
    mock_tenant_context: AsyncMock,
    mock_bus: AsyncMock,
    mock_engine: AsyncMock,
    pipeline_rows: MagicMock,
) -> None:
    """Verify that the embedder is invoked and result is published to events.clustering.pending."""
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_tenant_context.return_value = mock_context

    mock_store_instance = AsyncMock()
    mock_store_instance.save_event.return_value = True
    mock_store_cls.return_value = mock_store_instance

    # Mock embedder
    mock_embedder = AsyncMock()
    mock_embedder.embed_text.return_value = [0.1] * 1536

    event = make_canonical_event()
    pipeline_rows.claim.return_value = ClaimedEvent(event, 0, datetime.now(UTC))
    consumer = PipelineConsumer(mock_bus, mock_engine, embedder=mock_embedder)

    await consumer.process_message(event)

    mock_embedder.embed_text.assert_called_once_with(event.content_clean)

    # Assert bus.publish was called with topic events.clustering.pending and updated event
    publish_calls = mock_bus.publish.call_args_list
    clustering_call = None
    for call in publish_calls:
        if call[0][0] == "events.clustering.pending":
            clustering_call = call
            break

    assert clustering_call is not None
    published_event = clustering_call[0][1]
    assert published_event.metadata.get("embedding") == [0.1] * 1536
    pipeline_rows.mark_done.assert_awaited_once()


def _context(mock_tenant_context: AsyncMock) -> None:
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = AsyncMock()
    mock_tenant_context.return_value = mock_context


@pytest.mark.asyncio
@patch("checkcord.pipeline.consumer.tenant_context")
@patch("checkcord.pipeline.consumer.PostgresEventStore")
async def test_embedding_failure_keeps_event_and_does_not_raise(
    mock_store_cls: AsyncMock,
    mock_tenant_context: AsyncMock,
    mock_bus: AsyncMock,
    mock_engine: AsyncMock,
    pipeline_rows: MagicMock,
) -> None:
    _context(mock_tenant_context)
    mock_store_cls.return_value = AsyncMock()
    mock_store_cls.return_value.save_event.return_value = True
    embedder = AsyncMock()
    embedder.embed_text.side_effect = RuntimeError("provider down")
    event = make_canonical_event()
    claimed = ClaimedEvent(event, 2, datetime.now(UTC))
    pipeline_rows.claim.return_value = claimed

    await PipelineConsumer(mock_bus, mock_engine, embedder=embedder).process_message(event)

    mock_store_cls.return_value.save_event.assert_awaited_once_with(event, pipeline_pending=True)
    pipeline_rows.mark_attempt_failed.assert_awaited_once()
    assert pipeline_rows.mark_attempt_failed.call_args.args[2] is claimed
    pipeline_rows.mark_done.assert_not_called()
    mock_bus.publish.assert_not_called()


@pytest.mark.asyncio
@patch("checkcord.pipeline.consumer.tenant_context")
@patch("checkcord.pipeline.consumer.PostgresEventStore")
async def test_duplicate_delivery_still_finishes_unfinished_row(
    mock_store_cls: AsyncMock,
    mock_tenant_context: AsyncMock,
    mock_bus: AsyncMock,
    mock_engine: AsyncMock,
    pipeline_rows: MagicMock,
) -> None:
    _context(mock_tenant_context)
    mock_store_cls.return_value = AsyncMock()
    mock_store_cls.return_value.save_event.return_value = False
    embedder = AsyncMock()
    embedder.embed_text.return_value = [0.2] * 8
    event = make_canonical_event(source_type=SourceType.PR_OPENED)
    pipeline_rows.claim.return_value = ClaimedEvent(event, 1, datetime.now(UTC))

    await PipelineConsumer(mock_bus, mock_engine, embedder=embedder).process_message(event)

    mock_bus.publish.assert_awaited_once()
    assert mock_bus.publish.call_args.args[0] == "events.clustering.pending"
    pipeline_rows.mark_done.assert_awaited_once()


@pytest.mark.asyncio
@patch("checkcord.pipeline.consumer.tenant_context")
async def test_finish_event_without_embedder_marks_done(
    mock_tenant_context: AsyncMock,
    mock_bus: AsyncMock,
    mock_engine: AsyncMock,
    pipeline_rows: MagicMock,
) -> None:
    _context(mock_tenant_context)
    event = make_canonical_event()
    pipeline_rows.claim.return_value = ClaimedEvent(event, 0, datetime.now(UTC))

    assert await PipelineConsumer(mock_bus, mock_engine).finish_event(
        event.tenant_id, event.event_id
    )
    pipeline_rows.mark_done.assert_awaited_once()
    mock_bus.publish.assert_not_called()


@pytest.mark.asyncio
@patch("checkcord.pipeline.consumer.list_tenant_ids")
@patch("checkcord.pipeline.consumer.tenant_context")
async def test_sweep_unfinished_finishes_due_rows_per_tenant(
    mock_tenant_context: AsyncMock,
    mock_list_tenants: AsyncMock,
    mock_bus: AsyncMock,
    mock_engine: AsyncMock,
    pipeline_rows: MagicMock,
) -> None:
    _context(mock_tenant_context)
    good, broken = uuid4(), uuid4()
    mock_list_tenants.return_value = [broken, good]
    due = [uuid4(), uuid4()]

    async def due_ids(conn: Any, tenant_id: Any, now: Any = None) -> list[Any]:
        if tenant_id == broken:
            raise RuntimeError("boom")
        return due

    pipeline_rows.due_event_ids = AsyncMock(side_effect=due_ids)
    pipeline_rows.claim.side_effect = lambda conn, tenant_id, event_id, now=None: ClaimedEvent(
        make_canonical_event(), 0, datetime.now(UTC)
    )

    assert await PipelineConsumer(mock_bus, mock_engine).sweep_unfinished() == 2
    assert pipeline_rows.mark_done.await_count == 2
