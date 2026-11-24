import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.models.raw_payloads import RawChatPayload

# fakeredis freezes the loop on blocking XREADGROUP and mis-reports XAUTOCLAIM cursors.


@pytest.fixture
def valid_event() -> CanonicalEvent:
    return CanonicalEvent(
        tenant_id=uuid4(),
        source_platform=SourcePlatform.SLACK,
        source_type=SourceType.MESSAGE,
        source_id="msg_123",
        happened_at=datetime(2023, 1, 1, 12, 0, tzinfo=UTC),
        author={"source_id": "user_456"},
        content_raw='{"text": "hello"}',
        content_clean="hello",
        access_control={"is_public": False},
    )


@pytest.mark.asyncio
async def test_ensure_group_idempotent(async_redis_client: Redis) -> None:
    bus = RedisStreamsEventBus(async_redis_client)
    topic = "test_topic"
    group = "test_group"

    # First call should succeed
    await bus._ensure_group(topic, group)
    # Second call should safely ignore BUSYGROUP
    await bus._ensure_group(topic, group)

    # Assert stream exists by checking its length
    assert await async_redis_client.xlen(topic) == 0


@pytest.mark.asyncio
async def test_publish(async_redis_client: Redis, valid_event: CanonicalEvent) -> None:
    bus = RedisStreamsEventBus(async_redis_client)
    topic = "test_topic_pub"

    msg_id = await bus.publish(topic, valid_event)
    assert msg_id is not None

    # Verify it exists in Redis
    messages = await async_redis_client.xrange(topic)
    assert messages is not None
    assert len(messages) == 1

    msg_id_out = messages[0][0]
    assert isinstance(msg_id_out, bytes)
    assert msg_id_out.decode("utf-8") == msg_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subscribe_success(async_redis_client: Redis, valid_event: CanonicalEvent) -> None:
    bus = RedisStreamsEventBus(async_redis_client)
    topic = "test_topic_sub"
    group = "test_group_sub"

    # We will use an asyncio.Event to coordinate
    handled_event = asyncio.Event()
    received_events = []

    async def mock_handler(event: CanonicalEvent) -> None:
        received_events.append(event)
        handled_event.set()

    # Pre-create group so publish doesn't create it without the group knowing
    await bus._ensure_group(topic, group)

    # Publish event
    await bus.publish(topic, valid_event)

    # Run subscriber in background
    subscriber_task = asyncio.create_task(bus.subscribe(topic, group, "consumer1", mock_handler))

    # Wait for handler to process
    try:
        await asyncio.wait_for(handled_event.wait(), timeout=5.0)
    finally:
        subscriber_task.cancel()

    assert len(received_events) == 1
    assert received_events[0].source_id == valid_event.source_id

    # Verify message is acknowledged (PEL should be empty)
    import typing

    xpending: typing.Any = async_redis_client.xpending
    pending = await xpending(topic, group)
    assert pending["pending"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subscribe_dlq_on_poison_pill(async_redis_client: Redis) -> None:
    bus = RedisStreamsEventBus(async_redis_client)
    topic = "test_topic_poison"
    group = "test_group_poison"
    dlq_topic = f"{topic}.dlq"

    await bus._ensure_group(topic, group)

    # Publish a poison pill (invalid payload)
    await async_redis_client.xadd(topic, {"bad": "data"})

    async def mock_handler(event: CanonicalEvent) -> None:
        pass  # Should never be reached

    # Run subscriber for a short duration
    subscriber_task = asyncio.create_task(bus.subscribe(topic, group, "consumer1", mock_handler))

    # Give it time to poll and process
    await asyncio.sleep(0.5)
    subscriber_task.cancel()

    # Verify message moved to DLQ
    dlq_messages = await async_redis_client.xrange(dlq_topic)
    assert dlq_messages is not None
    assert len(dlq_messages) == 1

    dlq_payload_data = dlq_messages[0][1]
    assert dlq_payload_data is not None
    assert "dlq_error" in {
        k.decode("utf-8") if isinstance(k, bytes) else str(k): v
        for k, v in dlq_payload_data.items()
    }

    # Verify main stream message is ACK'd (PEL is empty)
    import typing

    xpending: typing.Any = async_redis_client.xpending
    pending = await xpending(topic, group)
    assert pending["pending"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subscribe_dlq_on_handler_failure(
    async_redis_client: Redis, valid_event: CanonicalEvent
) -> None:
    bus = RedisStreamsEventBus(async_redis_client)
    topic = "test_topic_fail"
    group = "test_group_fail"
    dlq_topic = f"{topic}.dlq"

    await bus._ensure_group(topic, group)

    call_count = 0

    async def failing_handler(event: CanonicalEvent) -> None:
        nonlocal call_count
        call_count += 1
        raise ValueError("Simulated handler failure")

    msg_id = await bus.publish(topic, valid_event)

    # Get raw payload as read from Redis
    messages = await async_redis_client.xrange(topic)
    assert messages is not None
    payload_data = messages[0][1]
    assert payload_data is not None

    raw_payload = {
        k.decode("utf-8") if isinstance(k, bytes) else str(k): v.decode("utf-8")
        if isinstance(v, bytes)
        else str(v)
        for k, v in payload_data.items()
    }

    # Simulate delivery 4 times (initial + 3 retries from future PEL reclaimer)
    for _ in range(4):
        await bus._process_message(topic, group, msg_id, raw_payload, failing_handler)

    assert call_count == 4

    # Verify message moved to DLQ
    dlq_messages = await async_redis_client.xrange(dlq_topic)
    assert dlq_messages is not None
    assert len(dlq_messages) == 1

    dlq_payload_data = dlq_messages[0][1]
    assert dlq_payload_data is not None

    dlq_payload = {
        k.decode("utf-8") if isinstance(k, bytes) else str(k): v.decode("utf-8")
        if isinstance(v, bytes)
        else str(v)
        for k, v in dlq_payload_data.items()
    }
    assert "dlq_error" in dlq_payload
    assert "Simulated handler failure" in dlq_payload["dlq_error"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subscribe_non_canonical_event_payload_type_raw_chat(
    async_redis_client: Redis,
) -> None:
    """A RawChatPayload must reach its handler when payload_type=RawChatPayload.

    _process_message once parsed every body as a CanonicalEvent, so a
    RawChatPayload on events.raw.chat failed to parse and went to the DLQ.
    """
    bus = RedisStreamsEventBus(async_redis_client)
    topic = "test_topic_raw_chat"
    group = "test_group_raw_chat"
    dlq_topic = f"{topic}.dlq"

    raw_chat_payload = RawChatPayload(
        tenant_id=uuid4(),
        platform=SourcePlatform.SLACK,
        channel_id="C123",
        author_id="U456",
        author_name="alice",
        message_id="msg_raw_1",
        happened_at=datetime.now(UTC),
        content_raw="hello team",
        content_clean="hello team",
        access_control={"is_public": True},
    )

    await bus._ensure_group(topic, group)
    await bus.publish(topic, raw_chat_payload)

    handled_event = asyncio.Event()
    received: list[RawChatPayload] = []

    async def mock_handler(event: RawChatPayload) -> None:
        received.append(event)
        handled_event.set()

    subscriber_task = asyncio.create_task(
        bus.subscribe(topic, group, "consumer1", mock_handler, payload_type=RawChatPayload)
    )
    try:
        await asyncio.wait_for(handled_event.wait(), timeout=5.0)
    finally:
        subscriber_task.cancel()

    assert len(received) == 1
    assert isinstance(received[0], RawChatPayload)
    assert received[0].message_id == "msg_raw_1"

    # Must not have been treated as a poison pill.
    dlq_messages = await async_redis_client.xrange(dlq_topic)
    assert not dlq_messages


@pytest.mark.asyncio
async def test_publish_does_not_trim(
    async_redis_client: Redis, valid_event: CanonicalEvent
) -> None:
    bus = RedisStreamsEventBus(async_redis_client)
    topic = "test_topic_publish_no_trim"
    for _ in range(300):
        await bus.publish(topic, valid_event)
    assert await async_redis_client.xlen(topic) == 300


# ---------------------------------------------------------------------------
# XAUTOCLAIM-based reclaimer for pending messages. subscribe() only polls new
# messages (id='>'), so without it a message that failed below max_retries
# would stay pending forever. These tests call _reclaim_once directly so they
# do not wait on the reclaim_interval_ms timer.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reclaim_once_claims_idle_pending_message_and_reprocesses(
    async_redis_client: Redis, valid_event: CanonicalEvent
) -> None:
    bus = RedisStreamsEventBus(async_redis_client)
    bus.reclaim_min_idle_ms = 0  # immediately eligible for this test
    topic = "test_topic_reclaim_success"
    group = "test_group_reclaim_success"

    await bus._ensure_group(topic, group)
    await bus.publish(topic, valid_event)

    # Deliver to consumer1 without acking -- simulates a consumer that read
    # the message and then died/failed before ack, leaving it pending.
    await async_redis_client.xreadgroup(
        groupname=group, consumername="consumer1", streams={topic: ">"}, count=10
    )

    received: list[CanonicalEvent] = []

    async def handler(event: CanonicalEvent) -> None:
        received.append(event)

    await bus._reclaim_once(topic, group, "consumer2", handler, CanonicalEvent)

    assert len(received) == 1
    assert received[0].source_id == valid_event.source_id

    import typing

    xpending: typing.Any = async_redis_client.xpending
    pending = await xpending(topic, group)
    assert pending["pending"] == 0, "reclaimed message must be acked after successful reprocessing"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reclaim_once_does_not_claim_message_within_min_idle_time(
    async_redis_client: Redis, valid_event: CanonicalEvent
) -> None:
    bus = RedisStreamsEventBus(async_redis_client)
    bus.reclaim_min_idle_ms = 60_000  # 60s -- nothing delivered moments ago should qualify
    topic = "test_topic_reclaim_too_young"
    group = "test_group_reclaim_too_young"

    await bus._ensure_group(topic, group)
    await bus.publish(topic, valid_event)

    await async_redis_client.xreadgroup(
        groupname=group, consumername="consumer1", streams={topic: ">"}, count=10
    )

    received: list[CanonicalEvent] = []

    async def handler(event: CanonicalEvent) -> None:
        received.append(event)

    await bus._reclaim_once(topic, group, "consumer2", handler, CanonicalEvent)

    assert received == [], "a message delivered moments ago must not be reclaimed yet"

    import typing

    xpending: typing.Any = async_redis_client.xpending
    pending = await xpending(topic, group)
    assert pending["pending"] == 1, "message must remain pending, owned by the original consumer"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reclaim_preserves_retry_counter_across_reclaim(
    async_redis_client: Redis, valid_event: CanonicalEvent
) -> None:
    """A reclaimed message must not get a fresh retry budget."""
    bus = RedisStreamsEventBus(async_redis_client)
    bus.reclaim_min_idle_ms = 0
    topic = "test_topic_reclaim_retry_counter"
    group = "test_group_reclaim_retry_counter"

    await bus._ensure_group(topic, group)
    msg_id = await bus.publish(topic, valid_event)

    messages = await async_redis_client.xrange(topic)
    assert messages is not None
    assert messages[0][1] is not None
    raw_payload = bus._decode_raw_payload(messages[0][1])

    # Deliver via XREADGROUP so the message actually enters the consumer
    # group's PEL -- _reclaim_once operates on the real PEL via XAUTOCLAIM,
    # unlike _process_message itself, which only tracks retries/acks and
    # doesn't require prior delivery.
    await async_redis_client.xreadgroup(
        groupname=group, consumername="consumer1", streams={topic: ">"}, count=10
    )

    async def failing_handler(event: CanonicalEvent) -> None:
        raise ValueError("Simulated transient failure")

    # First delivery+failure, as the original consumer would do.
    await bus._process_message(topic, group, msg_id, raw_payload, failing_handler)
    retry_key = f"retries:{topic}:{group}:{msg_id}"
    assert await async_redis_client.get(retry_key) == b"1"

    # Reclaim and fail again -- counter must continue from 1, not reset.
    await bus._reclaim_once(topic, group, "consumer2", failing_handler, CanonicalEvent)
    assert await async_redis_client.get(retry_key) == b"2"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reclaim_exceeding_max_retries_goes_to_dlq(
    async_redis_client: Redis, valid_event: CanonicalEvent
) -> None:
    bus = RedisStreamsEventBus(async_redis_client)
    bus.reclaim_min_idle_ms = 0
    topic = "test_topic_reclaim_dlq"
    group = "test_group_reclaim_dlq"
    dlq_topic = f"{topic}.dlq"

    await bus._ensure_group(topic, group)
    msg_id = await bus.publish(topic, valid_event)

    messages = await async_redis_client.xrange(topic)
    assert messages is not None
    assert messages[0][1] is not None
    raw_payload = bus._decode_raw_payload(messages[0][1])

    # Deliver via XREADGROUP so the message is in the real PEL for
    # _reclaim_once's XAUTOCLAIM to find.
    await async_redis_client.xreadgroup(
        groupname=group, consumername="consumer1", streams={topic: ">"}, count=10
    )

    async def failing_handler(event: CanonicalEvent) -> None:
        raise ValueError("Simulated persistent failure")

    # Drive the retry counter up to bus.max_retries via direct delivery
    # (as the original consumer's own poll loop would), then let the
    # reclaim path deliver the message that finally exceeds it.
    for _ in range(bus.max_retries):
        await bus._process_message(topic, group, msg_id, raw_payload, failing_handler)

    await bus._reclaim_once(topic, group, "consumer2", failing_handler, CanonicalEvent)

    dlq_messages = await async_redis_client.xrange(dlq_topic)
    assert dlq_messages is not None
    assert len(dlq_messages) == 1
    assert dlq_messages[0][1] is not None
    dlq_payload = bus._decode_raw_payload(dlq_messages[0][1])
    assert "Simulated persistent failure" in dlq_payload["dlq_error"]

    import typing

    xpending: typing.Any = async_redis_client.xpending
    pending = await xpending(topic, group)
    assert pending["pending"] == 0, "DLQ'd message must be acked, not left pending"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subscribe_starts_and_cleanly_cancels_reclaim_task(
    async_redis_client: Redis,
) -> None:
    """The reclaim sweep must run independently of the poll loop and must not
    leak a task or raise when subscribe() itself is cancelled."""
    bus = RedisStreamsEventBus(async_redis_client)
    bus.reclaim_interval_ms = 10  # fast enough to observe within the test
    topic = "test_topic_reclaim_wiring"
    group = "test_group_reclaim_wiring"

    reclaim_calls = 0
    original_reclaim_once = bus._reclaim_once

    async def counting_reclaim_once(*args: object, **kwargs: object) -> None:
        nonlocal reclaim_calls
        reclaim_calls += 1
        await original_reclaim_once(*args, **kwargs)

    bus._reclaim_once = counting_reclaim_once  # type: ignore[method-assign]

    async def handler(event: CanonicalEvent) -> None:
        pass

    subscriber_task = asyncio.create_task(bus.subscribe(topic, group, "consumer1", handler))
    await asyncio.sleep(0.2)
    subscriber_task.cancel()
    try:
        await subscriber_task
    except asyncio.CancelledError:
        pass

    assert reclaim_calls > 0, "reclaim loop must have run at least once given a 10ms interval"

    # No orphaned reclaim task left behind.
    await asyncio.sleep(0)
    remaining = [t for t in asyncio.all_tasks() if "_reclaim_loop" in repr(t.get_coro())]
    assert remaining == []


@pytest.mark.asyncio
async def test_handle_dlq_keeps_fields_and_adds_error(async_redis_client: Redis) -> None:
    bus = RedisStreamsEventBus(async_redis_client)
    topic, group = "test_topic_dlq_fields", "test_group_dlq_fields"
    await bus._ensure_group(topic, group)
    await bus._handle_dlq(topic, group, "1-0", {"data": "x"}, "simulated error")
    [(_, fields)] = await async_redis_client.xrange(f"{topic}.dlq")
    assert fields == {b"data": b"x", b"dlq_error": b"simulated error"}
