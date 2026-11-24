import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app_role_helpers import create_app_role, drop_app_role
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.bus import dead_letter
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core.config import get_settings
from checkcord.models.event import CanonicalEvent, SourcePlatform, SourceType
from checkcord.pipeline.consumer import PipelineConsumer
from checkcord.storage import event_pipeline
from checkcord.storage.postgres import (
    PostgresEventStore,
    create_checkcord_engine,
    events_table,
    tenant_context,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

CLUSTERING = "events.clustering.pending"


class FlakyEmbedder:
    def __init__(self, fail: bool = True, delay: float = 0.0) -> None:
        self.fail = fail
        self.delay = delay
        self.calls = 0

    async def embed_text(self, text: str) -> list[float]:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("embedding provider unavailable")
        return [0.25] * 8


def _event(tenant_id: UUID) -> CanonicalEvent:
    return CanonicalEvent(
        tenant_id=tenant_id,
        source_platform=SourcePlatform.CUSTOM,
        source_type=SourceType.MESSAGE,
        source_id=f"msg_{uuid4().hex[:8]}",
        happened_at=datetime.now(UTC),
        author={"source_id": "user_test"},
        content_raw="the deploy runbook lives in the ops repo",
        content_clean="the deploy runbook lives in the ops repo",
        access_control={"is_public": True},
    )


@pytest_asyncio.fixture
async def app_engine() -> AsyncGenerator[AsyncEngine, None]:
    role, url = await create_app_role()
    engine = create_checkcord_engine(url)
    try:
        yield engine
    finally:
        await engine.dispose()
        await drop_app_role(role)


@pytest_asyncio.fixture
async def tenant() -> AsyncGenerator[UUID, None]:
    tenant_id = uuid4()
    yield tenant_id
    owner = create_checkcord_engine(get_settings().database_url)
    try:
        async with owner.begin() as conn:
            await conn.execute(text("DELETE FROM events WHERE tenant_id = :t"), {"t": tenant_id})
    finally:
        await owner.dispose()


async def _row(engine: AsyncEngine, tenant_id: UUID, event_id: UUID) -> Any:
    c = events_table.c
    async with tenant_context(engine, tenant_id) as conn:
        result = await conn.execute(
            select(
                c.pipeline_state,
                c.pipeline_attempts,
                c.pipeline_next_attempt_at,
                c.pipeline_last_error,
            ).where(c.event_id == event_id)
        )
        rows = result.all()
    assert len(rows) <= 1
    return rows[0] if rows else None


def _consumer(engine: AsyncEngine, redis: Redis, embedder: Any) -> PipelineConsumer:
    return PipelineConsumer(RedisStreamsEventBus(redis), engine, embedder=embedder)


async def test_event_is_saved_when_embedding_fails(
    app_engine: AsyncEngine, async_redis_client: Redis, tenant: UUID
) -> None:
    event = _event(tenant)
    before = datetime.now(UTC)

    await _consumer(app_engine, async_redis_client, FlakyEmbedder()).process_message(event)

    row = await _row(app_engine, tenant, event.event_id)
    assert row.pipeline_state == event_pipeline.PENDING
    assert row.pipeline_attempts == 1
    assert "embedding provider unavailable" in row.pipeline_last_error
    assert row.pipeline_next_attempt_at >= before + event_pipeline.BACKOFF_BASE
    assert await async_redis_client.xlen(CLUSTERING) == 0


async def test_sweep_finishes_event_once_embedder_works(
    app_engine: AsyncEngine, async_redis_client: Redis, tenant: UUID
) -> None:
    event = _event(tenant)
    embedder = FlakyEmbedder()
    consumer = _consumer(app_engine, async_redis_client, embedder)
    await consumer.process_message(event)

    assert await consumer.sweep_tenant(tenant) == 0
    embedder.fail = False
    later = datetime.now(UTC) + timedelta(minutes=5)
    assert await consumer.sweep_tenant(tenant, now=later) == 1

    row = await _row(app_engine, tenant, event.event_id)
    assert (row.pipeline_state, row.pipeline_last_error) == (event_pipeline.DONE, None)
    [(_, fields)] = await async_redis_client.xrange(CLUSTERING)
    published = CanonicalEvent.from_redis_dict(RedisStreamsEventBus._decode_raw_payload(fields))
    assert published.event_id == event.event_id
    assert published.metadata["embedding"] == [0.25] * 8
    assert await consumer.sweep_tenant(tenant, now=later) == 0


async def test_redelivery_continues_unfinished_event_without_duplicates(
    app_engine: AsyncEngine, async_redis_client: Redis, tenant: UUID
) -> None:
    event = _event(tenant)
    async with tenant_context(app_engine, tenant) as conn:
        assert await PostgresEventStore(conn).save_event(event, pipeline_pending=True)

    embedder = FlakyEmbedder(fail=False)
    consumer = _consumer(app_engine, async_redis_client, embedder)
    await consumer.process_message(event)
    await consumer.process_message(event)

    async with tenant_context(app_engine, tenant) as conn:
        count = await conn.scalar(
            select(func.count()).select_from(events_table).where(events_table.c.tenant_id == tenant)
        )
    assert count == 1
    assert (await _row(app_engine, tenant, event.event_id)).pipeline_state == event_pipeline.DONE
    assert embedder.calls == 1
    assert await async_redis_client.xlen(CLUSTERING) == 1


async def test_two_workers_never_embed_the_same_event_at_once(
    app_engine: AsyncEngine, async_redis_client: Redis, tenant: UUID
) -> None:
    event = _event(tenant)
    async with tenant_context(app_engine, tenant) as conn:
        await PostgresEventStore(conn).save_event(event, pipeline_pending=True)

    embedder = FlakyEmbedder(fail=False, delay=0.3)
    a = _consumer(app_engine, async_redis_client, embedder)
    b = _consumer(app_engine, async_redis_client, embedder)
    results = await asyncio.gather(
        a.finish_event(tenant, event.event_id), b.finish_event(tenant, event.event_id)
    )

    assert sorted(results) == [False, True]
    assert embedder.calls == 1
    assert await async_redis_client.xlen(CLUSTERING) == 1


async def test_gives_up_after_window_and_retry_requeues(
    app_engine: AsyncEngine, async_redis_client: Redis, tenant: UUID
) -> None:
    event = _event(tenant)
    embedder = FlakyEmbedder()
    consumer = _consumer(app_engine, async_redis_client, embedder)
    await consumer.process_message(event)

    almost = datetime.now(UTC) + event_pipeline.RETRY_WINDOW - timedelta(minutes=1)
    await consumer.finish_event(tenant, event.event_id, now=almost)
    assert (await _row(app_engine, tenant, event.event_id)).pipeline_state == "pending"

    past = datetime.now(UTC) + event_pipeline.RETRY_WINDOW + timedelta(minutes=1)
    await consumer.finish_event(tenant, event.event_id, now=past)
    row = await _row(app_engine, tenant, event.event_id)
    assert (row.pipeline_state, row.pipeline_attempts) == (event_pipeline.FAILED, 3)
    assert await consumer.sweep_tenant(tenant, now=past + timedelta(days=1)) == 0

    async with tenant_context(app_engine, tenant) as conn:
        [failed] = await event_pipeline.list_unfinished(conn, tenant)
        assert failed.event_id == event.event_id and failed.source == "custom/message"
        assert await event_pipeline.count_unfinished(conn, tenant) == {"failed": 1, "pending": 0}
        assert await event_pipeline.retry_failed(conn, tenant, [uuid4()]) == []
        assert await event_pipeline.retry_failed(conn, tenant) == [event.event_id]

    embedder.fail = False
    assert await consumer.sweep_tenant(tenant) == 1
    assert (await _row(app_engine, tenant, event.event_id)).pipeline_state == event_pipeline.DONE


async def test_dead_letter_round_trip_and_replayed_message_is_processed(
    app_engine: AsyncEngine, async_redis_client: Redis, tenant: UUID
) -> None:
    redis = async_redis_client
    topic, dlq = "events.enriched.chat", "events.enriched.chat.dlq"
    bus = RedisStreamsEventBus(redis)
    kept, dropped = _event(tenant), _event(tenant)
    for event in (kept, dropped):
        await bus._handle_dlq(topic, "pipeline_workers", "0-1", event.to_redis_dict(), "boom")

    [stream] = await dead_letter.list_streams(redis)
    assert (stream.name, stream.entries) == (dlq, 2)
    first, second = await dead_letter.entries(redis, dlq)
    assert first.error == "boom" and first.fields["event_id"] == str(kept.event_id)

    assert await dead_letter.drop(redis, dlq, [second.entry_id]) == [second.entry_id]
    assert await dead_letter.replay(redis, dlq) == [first.entry_id]
    assert await redis.xlen(dlq) == 0

    consumer = PipelineConsumer(bus, app_engine, embedder=FlakyEmbedder(fail=False))
    bus.poll_timeout = 100
    task = asyncio.create_task(consumer.start([topic]))
    try:
        for _ in range(50):
            row = await _row(app_engine, tenant, kept.event_id)
            if row is not None and row.pipeline_state == event_pipeline.DONE:
                break
            await asyncio.sleep(0.1)
    finally:
        consumer.stop()
        await asyncio.wait_for(task, timeout=5)

    assert (await _row(app_engine, tenant, kept.event_id)).pipeline_state == event_pipeline.DONE
    assert await _row(app_engine, tenant, dropped.event_id) is None
    assert await redis.xlen(dlq) == 0
    pending = await redis.xpending(topic, consumer.group_name)
    assert pending["pending"] == 0
