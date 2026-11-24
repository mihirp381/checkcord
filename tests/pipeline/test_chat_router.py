import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from keyword_llm import KeywordLLMClient
from pubsub_test_helpers import assert_topic_not_dlqd, real_subscription
from redis.asyncio import Redis

from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.llm.client import LLMClient, LLMNotConfiguredError
from checkcord.models.enums import SourcePlatform
from checkcord.models.raw_payloads import RawChatPayload
from checkcord.pipeline.chat_router import ChatRouter
from checkcord.storage import redis_trees
from checkcord.storage.redis_trees import get_active_discourse_trees, get_channel_buffer_length

SLACK = SourcePlatform.SLACK


def make_raw_payload(
    tenant_id: Any = None,
    channel_id: str = "C_ROUTER_TEST",
    text: str = "Discussion about customer feature request",
    message_id: str | None = None,
    thread_ts: str | None = None,
) -> RawChatPayload:
    return RawChatPayload(
        tenant_id=tenant_id or uuid4(),
        platform=SourcePlatform.SLACK,
        channel_id=channel_id,
        author_id="U1234",
        author_name="alice",
        message_id=message_id or f"msg_{uuid4().hex[:6]}",
        thread_ts=thread_ts,
        happened_at=datetime.now(UTC),
        content_raw=text,
        content_clean=text,
        access_control={"is_public": True},
    )


@pytest.fixture
def mock_bus() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_llm_client() -> AsyncMock:
    client = AsyncMock(spec=LLMClient)
    client.assign_discourse_tree.return_value = {
        "tree_id": str(uuid4()),
        "domain_tag": "PROBLEM_SPACE",
        "summary": "Customer feature request discussion",
    }
    return client


@pytest.mark.asyncio
async def test_llm_client_assign_discourse_tree_requires_model() -> None:
    with pytest.raises(LLMNotConfiguredError):
        await LLMClient(model=None).assign_discourse_tree([], [{"content": "x"}])


@pytest.mark.asyncio
async def test_keyword_llm_assign_discourse_tree() -> None:
    res = await KeywordLLMClient().assign_discourse_tree(
        [], [{"content": "We have a critical bug in auth flow problem"}]
    )
    assert "tree_id" in res
    assert res["domain_tag"] == "PROBLEM_SPACE"
    assert "summary" in res


@pytest.mark.asyncio
@patch("checkcord.pipeline.chat_router.increment_counter")
async def test_five_buffered_messages_are_routed_at_once(
    mock_counter: MagicMock,
    mock_bus: AsyncMock,
    mock_llm_client: AsyncMock,
    async_redis_client: Redis,
) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    channel_id = "C_COUNT_SWEEP"
    router = ChatRouter(bus=mock_bus, redis=redis, llm_client=mock_llm_client)

    for i in range(4):
        await router.process_message(
            make_raw_payload(tenant_id=tenant_id, channel_id=channel_id, text=f"Message {i + 1}")
        )
    mock_llm_client.assign_discourse_tree.assert_not_called()
    assert await get_channel_buffer_length(redis, tenant_id, SLACK, channel_id) == 4

    await router.process_message(
        make_raw_payload(tenant_id=tenant_id, channel_id=channel_id, text="Message 5")
    )
    mock_llm_client.assign_discourse_tree.assert_called_once()
    assert await get_channel_buffer_length(redis, tenant_id, SLACK, channel_id) == 0
    [tree] = await get_active_discourse_trees(redis, tenant_id, SLACK, channel_id)
    assert tree["domain_tag"] == "PROBLEM_SPACE"
    assert tree["message_count"] == "5"
    mock_counter.assert_called_with(
        "chat_router_block_routed_total",
        tags={"domain_tag": "PROBLEM_SPACE", "platform": "slack"},
    )


@pytest.mark.asyncio
@patch("checkcord.pipeline.chat_router.increment_counter")
async def test_the_sweeper_routes_an_old_buffer(
    mock_counter: MagicMock,
    mock_bus: AsyncMock,
    mock_llm_client: AsyncMock,
    async_redis_client: Redis,
) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    channel_id = "C_TIME_SWEEP"
    router = ChatRouter(
        bus=mock_bus, redis=redis, llm_client=mock_llm_client, sweep_interval_seconds=0.05
    )
    for text in ("Stale msg 1", "Stale msg 2"):
        await router.process_message(
            make_raw_payload(tenant_id=tenant_id, channel_id=channel_id, text=text)
        )
    await redis.set(
        f"buffer_ts:{tenant_id}:slack:{channel_id}", str(datetime.now(UTC).timestamp() - 200)
    )

    task = asyncio.create_task(router._sweeper_loop())
    await asyncio.sleep(0.15)
    router.stop()
    await asyncio.wait_for(task, timeout=1.0)

    mock_llm_client.assign_discourse_tree.assert_called_once()
    assert await get_channel_buffer_length(redis, tenant_id, SLACK, channel_id) == 0


@pytest.mark.asyncio
@patch("checkcord.pipeline.chat_router.increment_counter")
async def test_an_invalid_payload_is_counted(
    mock_counter: MagicMock, mock_bus: AsyncMock, mock_llm_client: AsyncMock
) -> None:
    router = ChatRouter(bus=mock_bus, redis=AsyncMock(), llm_client=mock_llm_client)
    await router.process_message({"corrupted": "payload"})
    mock_counter.assert_called_with("chat_router_error_total", tags={"error": "validation_error"})


@pytest.mark.asyncio
async def test_the_router_sees_only_its_channels_discussions(
    async_redis_client: Redis,
) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    other_tree = str(uuid4())
    await redis_trees.create_or_update_discourse_tree(
        redis, tenant_id, other_tree, "HYBRID", [make_raw_payload(tenant_id, "C_OTHER")]
    )
    llm = AsyncMock(spec=LLMClient)
    llm.assign_discourse_tree.return_value = {
        "tree_id": other_tree,
        "domain_tag": "HYBRID",
        "summary": "s",
    }
    router = ChatRouter(bus=AsyncMock(), redis=redis, llm_client=llm)

    await redis_trees.push_to_channel_buffer(redis, make_raw_payload(tenant_id, "C_MINE"))
    await router.flush_buffer_block(tenant_id, SLACK, "C_MINE")

    assert llm.assign_discourse_tree.call_args.args[0] == []
    [mine] = await get_active_discourse_trees(redis, tenant_id, SLACK, "C_MINE")
    assert mine["tree_id"] != other_tree
    [other] = await get_active_discourse_trees(redis, tenant_id, SLACK, "C_OTHER")
    assert other["message_count"] == "1"


@pytest.mark.asyncio
async def test_a_reply_joins_its_thread_without_the_model(
    mock_bus: AsyncMock, mock_llm_client: AsyncMock, async_redis_client: Redis
) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    router = ChatRouter(bus=mock_bus, redis=redis, llm_client=mock_llm_client)
    root = make_raw_payload(tenant_id, message_id="1.0", text="Should we cap retries?")
    await redis_trees.push_to_channel_buffer(redis, root)
    await router.flush_buffer_block(tenant_id, SLACK, root.channel_id)
    [tree] = await get_active_discourse_trees(redis, tenant_id, SLACK, root.channel_id)

    reply = make_raw_payload(tenant_id, message_id="1.1", thread_ts="1.0", text="Yes, at 3")
    await router.process_message(reply)

    mock_llm_client.assign_discourse_tree.assert_called_once()
    assert await get_channel_buffer_length(redis, tenant_id, SLACK, root.channel_id) == 0
    [joined] = await get_active_discourse_trees(redis, tenant_id, SLACK, root.channel_id)
    assert joined["tree_id"] == tree["tree_id"]
    assert [m["message_id"] for m in json.loads(joined["messages"])] == ["1.0", "1.1"]


@pytest.mark.asyncio
async def test_a_buffered_first_message_joins_the_thread_its_reply_opened(
    mock_bus: AsyncMock, mock_llm_client: AsyncMock, async_redis_client: Redis
) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    router = ChatRouter(bus=mock_bus, redis=redis, llm_client=mock_llm_client)
    root = make_raw_payload(tenant_id, message_id="2.0", text="Pick a queue")
    await redis_trees.push_to_channel_buffer(redis, root)
    await router.process_message(
        make_raw_payload(tenant_id, message_id="2.1", thread_ts="2.0", text="Use SQS")
    )

    await router.flush_buffer_block(tenant_id, SLACK, root.channel_id)

    mock_llm_client.assign_discourse_tree.assert_not_called()
    [tree] = await get_active_discourse_trees(redis, tenant_id, SLACK, root.channel_id)
    assert sorted(m["message_id"] for m in json.loads(tree["messages"])) == ["2.0", "2.1"]
    assert await get_channel_buffer_length(redis, tenant_id, SLACK, root.channel_id) == 0


@pytest.mark.asyncio
async def test_the_buffer_is_kept_when_routing_fails(
    mock_bus: AsyncMock, async_redis_client: Redis
) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    llm = AsyncMock(spec=LLMClient)
    llm.assign_discourse_tree.side_effect = ConnectionError("model down")
    router = ChatRouter(bus=mock_bus, redis=redis, llm_client=llm)
    p = make_raw_payload(tenant_id)
    await redis_trees.push_to_channel_buffer(redis, p)

    with pytest.raises(ConnectionError):
        await router.flush_buffer_block(tenant_id, SLACK, p.channel_id)

    assert await get_channel_buffer_length(redis, tenant_id, SLACK, p.channel_id) == 1
    assert await get_active_discourse_trees(redis, tenant_id, SLACK, p.channel_id) == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_real_bus_delivers_scrubbed_chat(
    mock_llm_client: AsyncMock, async_redis_client: Redis
) -> None:
    redis = async_redis_client
    bus = RedisStreamsEventBus(redis)
    topic = "test_topic_scrubbed_chat_e2e"
    tenant_id = uuid4()
    channel_id = "C_E2E_REAL_BUS"
    payload = make_raw_payload(
        tenant_id=tenant_id, channel_id=channel_id, text="We decided to use Postgres"
    )
    router = ChatRouter(
        bus=bus,
        redis=redis,
        llm_client=mock_llm_client,
        group_name="chat_router_e2e_group",
        consumer_name="chat-router-e2e-1",
    )
    await bus.publish(topic=topic, event=payload)

    async with real_subscription(lambda: router._subscribe_to_topic(topic)):
        for _ in range(50):
            if await get_channel_buffer_length(redis, tenant_id, SLACK, channel_id) >= 1:
                break
            await asyncio.sleep(0.1)

    assert await get_channel_buffer_length(redis, tenant_id, SLACK, channel_id) == 1
    await assert_topic_not_dlqd(redis, topic)


@pytest.mark.asyncio
async def test_stop_ends_the_router(mock_bus: AsyncMock, mock_llm_client: AsyncMock) -> None:
    router = ChatRouter(
        bus=mock_bus, redis=AsyncMock(), llm_client=mock_llm_client, sweep_interval_seconds=10.0
    )

    async def infinite_subscribe(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(10)

    mock_bus.subscribe.side_effect = infinite_subscribe
    task = asyncio.create_task(router.start(["events.scrubbed.chat"]))
    await asyncio.sleep(0.05)
    assert not task.done()
    router.stop()
    await asyncio.wait_for(task, timeout=1.0)
