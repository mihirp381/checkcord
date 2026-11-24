import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from checkcord.models.enums import SourcePlatform
from checkcord.models.raw_payloads import RawChatPayload
from checkcord.storage import redis_trees
from checkcord.storage.redis_trees import (
    create_or_update_discourse_tree,
    drop_chat_state,
    get_active_channel_buffers,
    get_active_discourse_trees,
    get_channel_buffer_first_ts,
    get_channel_buffer_length,
    message_url,
    push_to_channel_buffer,
    read_channel_buffer,
    remove_discourse_tree,
    trim_channel_buffer,
)

SLACK = SourcePlatform.SLACK
PUBLIC: dict[str, Any] = {"is_public": True}
A, B, C = (str(uuid4()) for _ in range(3))


def make_raw_payload(
    tenant_id: Any = None,
    text: str = "Test chat content",
    channel_id: str = "C999",
    platform: SourcePlatform = SLACK,
    access_control: dict[str, Any] | None = None,
    author_id: str = "U123",
) -> RawChatPayload:
    return RawChatPayload(
        tenant_id=tenant_id or uuid4(),
        platform=platform,
        channel_id=channel_id,
        channel_name="eng",
        author_id=author_id,
        author_name="alice",
        message_id=f"1700000000.{uuid4().int % 10**6:06d}",
        thread_ts="1700000000.000001",
        happened_at=datetime.now(UTC),
        content_raw=text,
        content_clean=text,
        access_control=access_control or PUBLIC,
    )


def private(*ids: str) -> dict[str, Any]:
    return {"is_public": False, "allowed_identity_ids": list(ids)}


@pytest.mark.asyncio
async def test_buffering_flow(async_redis_client: Redis) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    channel_id = "C_TEST_BUFFER"

    assert await get_channel_buffer_length(redis, tenant_id, SLACK, channel_id) == 0
    assert await get_channel_buffer_first_ts(redis, tenant_id, SLACK, channel_id) is None

    p1 = make_raw_payload(tenant_id=tenant_id, text="Msg 1", channel_id=channel_id)
    assert await push_to_channel_buffer(redis, p1) == 1
    assert (await get_channel_buffer_first_ts(redis, tenant_id, SLACK, channel_id) or 0) > 0

    p2 = make_raw_payload(tenant_id=tenant_id, text="Msg 2", channel_id=channel_id)
    assert await push_to_channel_buffer(redis, p2) == 2
    assert (tenant_id, SLACK, channel_id) in await get_active_channel_buffers(redis)

    count, popped = await read_channel_buffer(redis, tenant_id, SLACK, channel_id)
    assert [p.content_clean for p in popped] == ["Msg 1", "Msg 2"]
    assert popped[0].access_control.is_public is True
    await trim_channel_buffer(redis, tenant_id, SLACK, channel_id, count)

    assert await get_channel_buffer_length(redis, tenant_id, SLACK, channel_id) == 0
    assert await get_channel_buffer_first_ts(redis, tenant_id, SLACK, channel_id) is None
    assert await get_active_channel_buffers(redis) == []


@pytest.mark.asyncio
async def test_buffers_are_kept_per_platform_and_channel(async_redis_client: Redis) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    await push_to_channel_buffer(redis, make_raw_payload(tenant_id, channel_id="C1"))
    await push_to_channel_buffer(redis, make_raw_payload(tenant_id, channel_id="C2"))
    await push_to_channel_buffer(
        redis, make_raw_payload(tenant_id, channel_id="C1", platform=SourcePlatform.MS_TEAMS)
    )

    count, payloads = await read_channel_buffer(redis, tenant_id, SLACK, "C1")
    assert len(payloads) == 1
    await trim_channel_buffer(redis, tenant_id, SLACK, "C1", count)
    assert sorted(await get_active_channel_buffers(redis)) == sorted(
        [(tenant_id, SLACK, "C2"), (tenant_id, SourcePlatform.MS_TEAMS, "C1")]
    )


@pytest.mark.asyncio
async def test_tree_keeps_citation_data_and_appends(async_redis_client: Redis) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    tree_id = str(uuid4())
    p1 = make_raw_payload(tenant_id, "Requirement for auth timeout", author_id="U1")
    p2 = make_raw_payload(tenant_id, "Set auth timeout to 30 mins", author_id="U2")

    await create_or_update_discourse_tree(
        redis, tenant_id, tree_id, "PROBLEM_SPACE", [p1, p2], summary="Auth timeout"
    )
    [tree] = await get_active_discourse_trees(redis, tenant_id, SLACK, "C999")
    assert tree["tree_id"] == tree_id
    assert (tree["platform"], tree["channel_id"], tree["channel_name"]) == ("slack", "C999", "eng")
    assert tree["message_count"] == "2"
    messages = json.loads(tree["messages"])
    assert [m["message_id"] for m in messages] == [p1.message_id, p2.message_id]
    assert [m["author_id"] for m in messages] == ["U1", "U2"]
    assert messages[0]["thread_ts"] == "1700000000.000001"
    assert messages[0]["url"] == message_url(p1)

    p3 = make_raw_payload(tenant_id, "Confirmed 30 mins timeout")
    await create_or_update_discourse_tree(redis, tenant_id, tree_id, "SOLUTION_SPACE", [p3])
    [updated] = await get_active_discourse_trees(redis, tenant_id, SLACK, "C999")
    assert updated["domain_tag"] == "SOLUTION_SPACE"
    assert updated["message_count"] == "3"


@pytest.mark.asyncio
async def test_tree_keeps_the_most_restrictive_audience(async_redis_client: Redis) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    tree_id = str(uuid4())

    await create_or_update_discourse_tree(
        redis, tenant_id, tree_id, "HYBRID", [make_raw_payload(tenant_id)]
    )
    [tree] = await get_active_discourse_trees(redis, tenant_id, SLACK, "C999")
    assert json.loads(tree["access_control"])["is_public"] is True

    await create_or_update_discourse_tree(
        redis,
        tenant_id,
        tree_id,
        "HYBRID",
        [
            make_raw_payload(tenant_id, access_control=private(A, B)),
            make_raw_payload(tenant_id, access_control=private(B, C)),
        ],
    )
    [tree] = await get_active_discourse_trees(redis, tenant_id, SLACK, "C999")
    audience = json.loads(tree["access_control"])
    assert audience["is_public"] is False
    assert audience["allowed_identity_ids"] == [B]
    assert audience["source_group_id"] == "C999"

    await create_or_update_discourse_tree(
        redis, tenant_id, tree_id, "HYBRID", [make_raw_payload(tenant_id)]
    )
    [tree] = await get_active_discourse_trees(redis, tenant_id, SLACK, "C999")
    assert json.loads(tree["access_control"])["allowed_identity_ids"] == [B]


@pytest.mark.asyncio
async def test_tree_rejects_messages_from_another_channel(async_redis_client: Redis) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    tree_id = str(uuid4())

    with pytest.raises(ValueError):
        await create_or_update_discourse_tree(
            redis,
            tenant_id,
            tree_id,
            "HYBRID",
            [
                make_raw_payload(tenant_id, channel_id="C1"),
                make_raw_payload(tenant_id, channel_id="C2"),
            ],
        )

    await create_or_update_discourse_tree(
        redis, tenant_id, tree_id, "HYBRID", [make_raw_payload(tenant_id, channel_id="C1")]
    )
    with pytest.raises(ValueError):
        await create_or_update_discourse_tree(
            redis, tenant_id, tree_id, "HYBRID", [make_raw_payload(tenant_id, channel_id="C2")]
        )
    with pytest.raises(ValueError):
        await create_or_update_discourse_tree(
            redis,
            tenant_id,
            tree_id,
            "HYBRID",
            [make_raw_payload(tenant_id, channel_id="C1", platform=SourcePlatform.MS_TEAMS)],
        )
    assert await get_active_discourse_trees(redis, tenant_id, SLACK, "C2") == []


@pytest.mark.asyncio
async def test_remove_discourse_tree_returns_it_and_clears_channel_set(
    async_redis_client: Redis,
) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    tree_id = str(uuid4())
    await create_or_update_discourse_tree(
        redis, tenant_id, tree_id, "HYBRID", [make_raw_payload(tenant_id)]
    )

    tree = await remove_discourse_tree(redis, tenant_id, tree_id)
    assert tree["tree_id"] == tree_id
    assert await get_active_discourse_trees(redis, tenant_id, SLACK, "C999") == []


def test_message_url() -> None:
    p = make_raw_payload()
    assert message_url(p) == f"https://slack.com/archives/C999/p{p.message_id.replace('.', '')}"
    teams = make_raw_payload(platform=SourcePlatform.MS_TEAMS)
    assert message_url(teams) is None
    teams = teams.model_copy(update={"raw_event": {"webUrl": "https://teams.example/msg"}})
    assert message_url(teams) == "https://teams.example/msg"


@pytest.mark.asyncio
async def test_drop_chat_state_by_channel_connection_and_author(async_redis_client: Redis) -> None:
    redis = async_redis_client
    tenant_id, other = uuid4(), uuid4()
    via = make_raw_payload(tenant_id, channel_id="C_VIA").model_copy(
        update={"connection": "slack:T1"}
    )
    await push_to_channel_buffer(redis, via)
    await push_to_channel_buffer(redis, make_raw_payload(tenant_id, channel_id="C_GONE"))
    await push_to_channel_buffer(redis, make_raw_payload(tenant_id, channel_id="C_KEEP"))
    await push_to_channel_buffer(redis, make_raw_payload(other, channel_id="C_GONE"))
    await create_or_update_discourse_tree(
        redis,
        tenant_id,
        "t_author",
        "PROBLEM_SPACE",
        [make_raw_payload(tenant_id, author_id="U_X")],
    )
    await create_or_update_discourse_tree(
        redis,
        tenant_id,
        "t_keep",
        "PROBLEM_SPACE",
        [make_raw_payload(tenant_id, channel_id="C_KEEP")],
    )

    assert await drop_chat_state(redis, tenant_id, channels={("slack", "C_GONE")}) == 1
    assert await drop_chat_state(redis, tenant_id, connection="slack:T1") == 1
    assert await drop_chat_state(redis, tenant_id, authors={"U_X"}) == 1

    assert set(await get_active_channel_buffers(redis)) == {
        (tenant_id, SLACK, "C_KEEP"),
        (other, SLACK, "C_GONE"),
    }
    assert await get_active_discourse_trees(redis, tenant_id, SLACK, "C999") == []
    [kept] = await get_active_discourse_trees(redis, tenant_id, SLACK, "C_KEEP")
    assert kept["tree_id"] == "t_keep"


async def _tree(
    redis: Redis, tenant_id: Any, *texts: str, due_ts: float = 0
) -> tuple[str, list[Any]]:
    tree_id = str(uuid4())
    payloads = [make_raw_payload(tenant_id, t) for t in texts]
    await create_or_update_discourse_tree(
        redis, tenant_id, tree_id, "HYBRID", payloads, due_ts=due_ts
    )
    return tree_id, payloads


@pytest.mark.asyncio
async def test_the_same_message_is_added_once(async_redis_client: Redis) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    tree_id, [p] = await _tree(redis, tenant_id, "Ship it")
    assert await create_or_update_discourse_tree(redis, tenant_id, tree_id, "HYBRID", [p]) == 0
    tree = await redis_trees.get_discourse_tree(redis, tenant_id, tree_id)
    assert tree["message_count"] == "1"


@pytest.mark.asyncio
async def test_a_thread_maps_to_its_waiting_discussion_only(async_redis_client: Redis) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    tree_id, _ = await _tree(redis, tenant_id, "Root")
    await redis_trees.map_thread(redis, tenant_id, SLACK, "C999", "1.0", tree_id)
    assert await redis_trees.thread_tree(redis, tenant_id, SLACK, "C999", "1.0") == tree_id
    await remove_discourse_tree(redis, tenant_id, tree_id)
    assert await redis_trees.thread_tree(redis, tenant_id, SLACK, "C999", "1.0") is None


@pytest.mark.asyncio
async def test_a_claim_is_a_lease_not_a_removal(async_redis_client: Redis) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    tree_id, _ = await _tree(redis, tenant_id, "Decision: use Postgres")
    assert await redis_trees.due_discussions(redis) == [(tenant_id, tree_id)]
    assert await redis_trees.claim_discussion(redis, tenant_id, tree_id)
    assert await redis_trees.claim_discussion(redis, tenant_id, tree_id) is None
    assert await redis_trees.due_discussions(redis) == []
    assert await redis_trees.due_discussions(redis, now=redis_trees._now() + 121)
    assert (await redis_trees.discussion_counts(redis, tenant_id))["waiting"] == 1


@pytest.mark.asyncio
async def test_finish_keeps_messages_that_arrived_meanwhile(async_redis_client: Redis) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    tree_id, [p] = await _tree(redis, tenant_id, "First")
    tree = await redis_trees.claim_discussion(redis, tenant_id, tree_id)
    assert tree is not None
    late = make_raw_payload(tenant_id, "Late")
    await create_or_update_discourse_tree(redis, tenant_id, tree_id, "HYBRID", [late])
    done = await redis_trees.finish_discussion(
        redis, tenant_id, tree_id, tree["version"], [p.message_id]
    )
    assert done is False
    [rest] = await get_active_discourse_trees(redis, tenant_id, SLACK, "C999")
    assert rest["tree_id"] != tree_id
    assert [m["message_id"] for m in json.loads(rest["messages"])] == [late.message_id]
    assert await redis_trees.get_discourse_tree(redis, tenant_id, tree_id) == {}


@pytest.mark.asyncio
async def test_transport_failures_back_off_and_unusable_replies_park(
    async_redis_client: Redis,
) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    tree_id, _ = await _tree(redis, tenant_id, "Decision")
    assert [redis_trees.retry_delay(n) for n in (1, 2, 3, 10)] == [30, 60, 120, 900]
    for _ in range(8):
        assert not await redis_trees.fail_discussion(
            redis, tenant_id, tree_id, "APIConnectionError: down", unusable=False
        )
    for n in range(redis_trees.UNUSABLE_LIMIT):
        parked = await redis_trees.fail_discussion(
            redis, tenant_id, tree_id, "UnusableReply: prose", unusable=True
        )
        assert parked is (n == redis_trees.UNUSABLE_LIMIT - 1)
    counts = await redis_trees.discussion_counts(redis, tenant_id)
    assert (counts["waiting"], counts["failed"]) == (0, 1)
    [failed] = await redis_trees.failed_discussions(redis, tenant_id)
    assert failed["last_error"] == "UnusableReply: prose"
    assert failed["attempts"] == "13"

    assert await redis_trees.retry_discussions(redis, tenant_id, [tree_id]) == 1
    assert await redis_trees.due_discussions(redis) == [(tenant_id, tree_id)]
    assert (await redis_trees.discussion_counts(redis, tenant_id))["failed"] == 0


@pytest.mark.asyncio
async def test_edits_and_removals_reach_waiting_messages(async_redis_client: Redis) -> None:
    redis = async_redis_client
    tenant_id = uuid4()
    tree_id, [a, b] = await _tree(redis, tenant_id, "Use 3 retries", "Agreed")
    buffered = make_raw_payload(tenant_id, "Buffered")
    await push_to_channel_buffer(redis, buffered)
    version = (await redis_trees.get_discourse_tree(redis, tenant_id, tree_id))["version"]

    edited = a.model_copy(update={"content_raw": "Use 5 retries", "content_clean": "Use 5 retries"})
    assert await redis_trees.update_message(redis, edited)
    tree = await redis_trees.get_discourse_tree(redis, tenant_id, tree_id)
    assert json.loads(tree["messages"])[0]["content"] == "Use 5 retries"
    assert tree["version"] != version

    removed = await redis_trees.remove_messages(
        redis, tenant_id, SLACK, "C999", [b.message_id, buffered.message_id]
    )
    assert removed == 2
    assert await get_channel_buffer_length(redis, tenant_id, SLACK, "C999") == 0
    tree = await redis_trees.get_discourse_tree(redis, tenant_id, tree_id)
    assert [m["message_id"] for m in json.loads(tree["messages"])] == [a.message_id]
    await redis_trees.remove_messages(redis, tenant_id, SLACK, "C999", [a.message_id])
    assert await redis_trees.get_discourse_tree(redis, tenant_id, tree_id) == {}
