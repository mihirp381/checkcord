import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
import openai
import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from checkcord.llm.client import LLMClient
from checkcord.llm.outcomes import Outcome, UnusableReply
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.raw_payloads import RawChatPayload
from checkcord.pipeline.chat_extractor import (
    ChatExtractor,
    build_outcome_event,
    is_transport_error,
)
from checkcord.storage import redis_trees

SLACK = SourcePlatform.SLACK
START = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)


def payload(tenant_id: UUID, channel_id: str, n: int, author_id: str = "U1") -> RawChatPayload:
    return RawChatPayload(
        tenant_id=tenant_id,
        platform=SLACK,
        channel_id=channel_id,
        channel_name=f"name-{channel_id}",
        author_id=author_id,
        author_name=f"name-{author_id}",
        message_id=f"1700000000.00000{n}",
        thread_ts="1700000000.000001",
        happened_at=START + timedelta(minutes=n),
        content_raw=f"message {n}",
        content_clean=f"message {n}",
        access_control={"is_public": True},
    )


def outcome(statement: str = "Card payments retry up to three times.", **kw: Any) -> Outcome:
    return Outcome(
        kind=kw.get("kind", "requirement"),
        statement=statement,
        details=kw.get("details", ()),
        messages=kw.get("messages", ()),
        replaces_earlier=kw.get("replaces_earlier", False),
    )


def model(settled: str = "SETTLED") -> AsyncMock:
    llm = AsyncMock(spec=LLMClient)
    llm.discussion_text = lambda messages: "\n".join(m["content"] for m in messages)
    llm.judge_settled.return_value = settled
    return llm


async def discussion(redis: Redis, tenant_id: UUID, channel_id: str = "C1", n: int = 1) -> str:
    tree_id = str(uuid4())
    await redis_trees.create_or_update_discourse_tree(
        redis, tenant_id, tree_id, "HYBRID", [payload(tenant_id, channel_id, n)], due_ts=0
    )
    return tree_id


async def make_due(redis: Redis) -> None:
    for raw in await redis.zrange(redis_trees.ACTIVE_TREES_ZSET, 0, -1):
        found = redis_trees.parse_member(raw)
        assert found is not None
        await redis.delete(f"discussion_lease:{found[0]}:{found[1]}")
        await redis.zadd(redis_trees.ACTIVE_TREES_ZSET, {raw: 0})


def extractor(redis: Redis, llm: Any, bus: Any = None) -> ChatExtractor:
    return ChatExtractor(bus=bus or AsyncMock(), redis=redis, llm_client=llm)


def test_an_outcome_event_cites_its_messages_and_carries_the_audience() -> None:
    tenant_id = uuid4()
    members = [str(uuid4())]
    tree = {
        "tree_id": "t1",
        "platform": "slack",
        "channel_id": "C1",
        "channel_name": "billing",
        "messages": json.dumps(
            [
                {
                    "message_id": f"m{i}",
                    "thread_ts": "th1",
                    "author_id": author,
                    "author_name": author.lower(),
                    "author_email": None,
                    "happened_at": (START + timedelta(minutes=i)).isoformat(),
                    "url": f"https://slack.com/archives/C1/pm{i}",
                }
                for i, author in enumerate(["U1", "U2", "U3"])
            ]
        ),
        "access_control": json.dumps(
            {"is_public": False, "allowed_identity_ids": members, "source_group_id": "C1"}
        ),
    }
    found = outcome(
        "Use `quote_total` for every quote.",
        kind="decision",
        details=("Rounds half up.",),
        messages=(1, 2),
        replaces_earlier=True,
    )

    event = build_outcome_event(tenant_id, tree, found, 2)

    assert event.source_platform == SLACK
    assert event.source_type == SourceType.MESSAGE
    assert event.source_id == "chat-tree:t1:2"
    assert event.content_clean == "Use `quote_total` for every quote.\n- Rounds half up."
    assert event.author.source_id == "U2"
    assert event.happened_at == START + timedelta(minutes=1)
    assert event.access_control.is_public is False
    assert [str(u) for u in event.access_control.allowed_identity_ids] == members
    assert event.audience_container == "C1"
    assert event.metadata["context_type"] == "adr"
    assert event.metadata["states_replacement"] is True
    source = event.metadata["chat_source"]
    assert source["channel_name"] == "billing"
    assert source["message_ids"] == ["m0", "m1"]
    assert source["discussion_message_ids"] == ["m0", "m1", "m2"]
    assert source["thread_ids"] == ["th1"]
    assert source["authors"] == ["U1", "U2"]
    assert source["first_message_at"] == START.isoformat()
    assert source["url"] == "https://slack.com/archives/C1/pm0"
    assert event.chat_metadata is not None and event.chat_metadata.reply_count == 1


def test_kinds_map_to_record_types() -> None:
    tree = {"tree_id": "t", "platform": "slack", "channel_id": "C", "messages": "[]"}
    types = [
        build_outcome_event(uuid4(), tree, outcome(kind=k), 1).metadata["context_type"]
        for k in ("decision", "constraint", "requirement", "risk")
    ]
    assert types == ["adr", "constraint", "business_requirement", "risk"]


def test_an_outcome_without_an_audience_is_private_and_empty() -> None:
    tree = {"tree_id": "t2", "platform": "ms_teams", "channel_id": "19:x", "messages": "[]"}
    event = build_outcome_event(uuid4(), tree, outcome(), 1)
    assert event.access_control.is_public is False
    assert event.access_control.allowed_identity_ids == []
    assert event.author.source_id == "unknown"


def test_transport_errors_are_told_apart_from_bad_replies() -> None:
    request = httpx.Request("POST", "http://model")
    for error in (
        openai.APIConnectionError(request=request),
        httpx.ConnectTimeout("slow"),
        TimeoutError(),
        RedisConnectionError(),
        httpx.HTTPStatusError("busy", request=request, response=httpx.Response(429)),
        httpx.HTTPStatusError("down", request=request, response=httpx.Response(503)),
    ):
        assert is_transport_error(error), error
    for error in (
        UnusableReply("prose"),
        ValueError("bad json"),
        httpx.HTTPStatusError("bad", request=request, response=httpx.Response(400)),
    ):
        assert not is_transport_error(error), error


@pytest.mark.asyncio
async def test_a_due_discussion_becomes_one_event_per_outcome(async_redis_client: Redis) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    tree_id = await discussion(redis, tenant_id)
    fresh = str(uuid4())
    await redis_trees.create_or_update_discourse_tree(
        redis, tenant_id, fresh, "HYBRID", [payload(tenant_id, "C1", 2)]
    )
    llm = model()
    llm.extract_outcomes.return_value = [outcome("First."), outcome("Second.", kind="risk")]
    bus = AsyncMock()

    assert await extractor(redis, llm, bus).poll_due_once() == 1

    published = [c.args for c in bus.publish.call_args_list]
    assert [t for t, _ in published] == ["events.raw.custom"] * 2
    assert [e.source_id for _, e in published] == [
        f"chat-tree:{tree_id}:1",
        f"chat-tree:{tree_id}:2",
    ]
    assert published[0][1].access_control.is_public is True
    assert await redis_trees.get_discourse_tree(redis, tenant_id, tree_id) == {}
    assert await redis_trees.get_discourse_tree(redis, tenant_id, fresh) != {}


@pytest.mark.asyncio
async def test_a_discussion_with_no_outcome_publishes_nothing_and_is_counted(
    async_redis_client: Redis,
) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    tree_id = await discussion(redis, tenant_id)
    llm = model()
    llm.extract_outcomes.return_value = []
    bus = AsyncMock()

    assert await extractor(redis, llm, bus).poll_due_once() == 1

    bus.publish.assert_not_called()
    assert await redis_trees.get_discourse_tree(redis, tenant_id, tree_id) == {}
    today = datetime.now(UTC).date().isoformat()
    assert await redis_trees.no_outcome_count(redis, tenant_id, [today]) == 1


@pytest.mark.asyncio
async def test_model_down_then_back_publishes_each_discussion_once(
    async_redis_client: Redis,
) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    trees = [await discussion(redis, tenant_id, f"C{i}", i) for i in range(1, 4)]
    llm = model()
    llm.extract_outcomes.side_effect = openai.APIConnectionError(
        request=httpx.Request("POST", "http://model")
    )
    bus = AsyncMock()
    worker = extractor(redis, llm, bus)

    for _ in range(3):
        assert await worker.poll_due_once() == 3
        await make_due(redis)
    bus.publish.assert_not_called()
    counts = await redis_trees.discussion_counts(redis, tenant_id)
    assert (counts["waiting"], counts["failed"]) == (3, 0)
    assert "APIConnectionError" in counts["error"]

    llm.extract_outcomes.side_effect = None
    llm.extract_outcomes.return_value = [outcome()]
    assert await worker.poll_due_once() == 3
    await make_due(redis)
    assert await worker.poll_due_once() == 0

    ids = sorted(c.args[1].source_id for c in bus.publish.call_args_list)
    assert ids == sorted(f"chat-tree:{t}:1" for t in trees)
    assert (await redis_trees.discussion_counts(redis, tenant_id))["waiting"] == 0


@pytest.mark.asyncio
async def test_a_crash_after_publishing_republishes_the_same_events(
    async_redis_client: Redis,
) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    tree_id = await discussion(redis, tenant_id)
    llm = model()
    llm.extract_outcomes.return_value = [outcome()]
    bus = AsyncMock()
    worker = extractor(redis, llm, bus)

    with (
        patch.object(redis_trees, "finish_discussion", side_effect=RuntimeError("killed")),
        pytest.raises(RuntimeError),
    ):
        await worker.poll_due_once()
    assert await worker.poll_due_once() == 0
    await make_due(redis)
    assert await worker.poll_due_once() == 1

    assert llm.extract_outcomes.await_count == 1
    first, second = (c.args[1] for c in bus.publish.call_args_list)
    assert first.source_id == second.source_id == f"chat-tree:{tree_id}:1"
    assert first.content_clean == second.content_clean
    assert first.happened_at == second.happened_at
    assert await redis_trees.get_discourse_tree(redis, tenant_id, tree_id) == {}


@pytest.mark.asyncio
async def test_unusable_replies_park_the_discussion_until_retried(
    async_redis_client: Redis,
) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    tree_id = await discussion(redis, tenant_id)
    llm = model()
    llm.extract_outcomes.side_effect = UnusableReply("reply has no outcomes list")
    worker = extractor(redis, llm)

    for _ in range(redis_trees.UNUSABLE_LIMIT):
        assert await worker.poll_due_once() == 1
        await make_due(redis)
    assert await worker.poll_due_once() == 0
    counts = await redis_trees.discussion_counts(redis, tenant_id)
    assert (counts["waiting"], counts["failed"]) == (0, 1)
    [parked] = await redis_trees.failed_discussions(redis, tenant_id)
    assert parked["tree_id"] == tree_id
    assert parked["last_error"].startswith("UnusableReply")

    llm.extract_outcomes.side_effect = None
    llm.extract_outcomes.return_value = [outcome()]
    assert await redis_trees.retry_discussions(redis, tenant_id, None) == 1
    assert await worker.poll_due_once() == 1
    assert await redis_trees.failed_discussions(redis, tenant_id) == []


@pytest.mark.asyncio
async def test_a_failed_publish_retries_without_asking_the_model_again(
    async_redis_client: Redis,
) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    tree_id = await discussion(redis, tenant_id)
    llm = model()
    llm.extract_outcomes.return_value = [outcome()]
    bus = AsyncMock()
    bus.publish.side_effect = [RedisConnectionError("bus down"), "1-0"]
    worker = extractor(redis, llm, bus)

    assert await worker.poll_due_once() == 1
    tree = await redis_trees.get_discourse_tree(redis, tenant_id, tree_id)
    assert tree["unusable"] == "0"
    await make_due(redis)
    assert await worker.poll_due_once() == 1

    assert llm.extract_outcomes.await_count == 1
    assert bus.publish.await_count == 2
    assert await redis_trees.get_discourse_tree(redis, tenant_id, tree_id) == {}


@pytest.mark.asyncio
async def test_an_outcome_the_discussion_did_not_settle_is_dropped_and_counted(
    async_redis_client: Redis,
) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    tree_id = await discussion(redis, tenant_id)
    llm = model("OPEN")
    llm.extract_outcomes.return_value = [outcome("Move everything into a monorepo.")]
    bus = AsyncMock()

    assert await extractor(redis, llm, bus).poll_due_once() == 1

    bus.publish.assert_not_called()
    day = datetime.now(UTC).date().isoformat()
    assert await redis_trees.not_settled_count(redis, tenant_id, [day]) == 1
    assert await redis_trees.get_discourse_tree(redis, tenant_id, tree_id) == {}


@pytest.mark.asyncio
async def test_a_transport_error_in_the_settled_check_leaves_the_discussion_waiting(
    async_redis_client: Redis,
) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    tree_id = await discussion(redis, tenant_id)
    llm = model()
    llm.extract_outcomes.return_value = [outcome()]
    llm.judge_settled.side_effect = openai.APIConnectionError(
        request=httpx.Request("POST", "http://model")
    )
    bus = AsyncMock()

    await extractor(redis, llm, bus).poll_due_once()

    bus.publish.assert_not_called()
    assert await redis_trees.get_discourse_tree(redis, tenant_id, tree_id) != {}
