"""Redis storage for channel buffers and discussions.

A buffer and a discussion each belong to one tenant, platform and channel. A discussion's score
in ``ACTIVE_TREES_ZSET`` is the time it is due for extraction; a discussion that kept getting
unusable replies is parked in ``FAILED_TREES_ZSET``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import structlog
from redis.asyncio import Redis
from redis.exceptions import WatchError

from checkcord.core.audience import combine_audiences
from checkcord.models.common import AccessControl
from checkcord.models.enums import AccessVisibility, AclType, SourcePlatform
from checkcord.models.raw_payloads import RawChatPayload

logger = structlog.get_logger(__name__)

ACTIVE_BUFFERS_SET = "active_channel_buffers"
ACTIVE_TREES_ZSET = "active_discourse_trees"
FAILED_TREES_ZSET = "failed_discussions"
LEASE_SECONDS = 120
THREAD_TTL_SECONDS = 7 * 24 * 3600
UNUSABLE_LIMIT = 5
ERROR_CHARS = 300


def _channel_ref(tenant_id: UUID | str, platform: SourcePlatform | str, channel_id: str) -> str:
    return f"{tenant_id}:{SourcePlatform(platform).value}:{channel_id}"


def _buffer_key(tenant_id: UUID | str, platform: SourcePlatform | str, channel_id: str) -> str:
    return f"buffer:{_channel_ref(tenant_id, platform, channel_id)}"


def _buffer_ts_key(tenant_id: UUID | str, platform: SourcePlatform | str, channel_id: str) -> str:
    return f"buffer_ts:{_channel_ref(tenant_id, platform, channel_id)}"


def _buffer_lock_key(tenant_id: UUID | str, platform: SourcePlatform | str, channel_id: str) -> str:
    return f"buffer_lock:{_channel_ref(tenant_id, platform, channel_id)}"


def _tree_key(tenant_id: UUID | str, tree_id: str) -> str:
    return f"discourse_tree:{tenant_id}:{tree_id}"


def _lease_key(tenant_id: UUID | str, tree_id: str) -> str:
    return f"discussion_lease:{tenant_id}:{tree_id}"


def _thread_key(
    tenant_id: UUID | str, platform: SourcePlatform | str, channel_id: str, thread_ts: str
) -> str:
    return f"chat_thread:{_channel_ref(tenant_id, platform, channel_id)}:{thread_ts}"


def _channel_trees_set(
    tenant_id: UUID | str, platform: SourcePlatform | str, channel_id: str
) -> str:
    return f"channel_trees:{_channel_ref(tenant_id, platform, channel_id)}"


def _member(tenant_id: UUID | str, tree_id: str) -> str:
    return f"{tenant_id}:{tree_id}"


def _decode(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _now() -> float:
    return datetime.now(UTC).timestamp()


def message_url(payload: RawChatPayload) -> str | None:
    if payload.platform == SourcePlatform.SLACK:
        ts = payload.message_id.replace(".", "")
        return f"https://slack.com/archives/{payload.channel_id}/p{ts}"
    url = payload.raw_event.get("webUrl")
    return str(url) if url else None


def message_entry(payload: RawChatPayload) -> dict[str, Any]:
    return {
        "message_id": payload.message_id,
        "thread_ts": payload.thread_ts,
        "author_id": payload.author_id,
        "author_name": payload.author_name,
        "author_email": payload.author_email,
        "content": payload.content_clean,
        "happened_at": payload.happened_at.isoformat(),
        "url": message_url(payload),
    }


def merge_channel_audience(channel_id: str, audiences: list[dict[str, Any]]) -> AccessControl:
    """The most restrictive audience across a channel's messages."""
    combined = combine_audiences(audiences)
    if combined["is_public"]:
        return AccessControl(
            is_public=True,
            visibility=AccessVisibility.PUBLIC,
            acl_type=AclType.OPEN,
            source_group_id=channel_id,
        )
    return AccessControl(
        is_public=False,
        visibility=AccessVisibility.PRIVATE,
        acl_type=AclType.CHANNEL_MEMBERS,
        allowed_identity_ids=combined["allowed_identity_ids"],
        source_group_id=channel_id,
    )


async def push_to_channel_buffer(redis: Redis, payload: RawChatPayload) -> int:
    """Append a payload to its channel's buffer. Returns the new length."""
    tenant_id, platform, channel_id = payload.tenant_id, payload.platform, payload.channel_id
    async with redis.pipeline(transaction=True) as pipe:
        pipe.rpush(_buffer_key(tenant_id, platform, channel_id), payload.model_dump_json())
        pipe.setnx(_buffer_ts_key(tenant_id, platform, channel_id), str(_now()))
        pipe.sadd(ACTIVE_BUFFERS_SET, _channel_ref(tenant_id, platform, channel_id))
        results = await pipe.execute()
    return int(results[0])


async def get_channel_buffer_length(
    redis: Redis, tenant_id: UUID, platform: SourcePlatform, channel_id: str
) -> int:
    return int(await redis.llen(_buffer_key(tenant_id, platform, channel_id)))


async def get_channel_buffer_first_ts(
    redis: Redis, tenant_id: UUID, platform: SourcePlatform, channel_id: str
) -> float | None:
    """POSIX time when the first message entered the channel buffer."""
    val = await redis.get(_buffer_ts_key(tenant_id, platform, channel_id))
    if val is None:
        return None
    try:
        return float(_decode(val))
    except (ValueError, TypeError):
        return None


async def lock_channel_buffer(
    redis: Redis, tenant_id: UUID, platform: SourcePlatform, channel_id: str, seconds: int = 60
) -> bool:
    key = _buffer_lock_key(tenant_id, platform, channel_id)
    return bool(await redis.set(key, "1", nx=True, ex=seconds))


async def unlock_channel_buffer(
    redis: Redis, tenant_id: UUID, platform: SourcePlatform, channel_id: str
) -> None:
    await redis.delete(_buffer_lock_key(tenant_id, platform, channel_id))


def _payloads(items: Iterable[Any]) -> list[RawChatPayload]:
    payloads: list[RawChatPayload] = []
    for item in items:
        try:
            payloads.append(RawChatPayload.model_validate_json(_decode(item)))
        except Exception as e:
            logger.error("redis_trees.payload_deserialization_failed", error=str(e))
    return payloads


async def read_channel_buffer(
    redis: Redis, tenant_id: UUID, platform: SourcePlatform, channel_id: str
) -> tuple[int, list[RawChatPayload]]:
    """The buffer's raw length and its readable payloads, left in place."""
    items = await redis.lrange(_buffer_key(tenant_id, platform, channel_id), 0, -1)
    return len(items), _payloads(items)


async def trim_channel_buffer(
    redis: Redis, tenant_id: UUID, platform: SourcePlatform, channel_id: str, count: int
) -> None:
    """Remove the first ``count`` entries; clear the buffer's bookkeeping once it is empty."""
    key = _buffer_key(tenant_id, platform, channel_id)
    while True:
        async with redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                left = int(await pipe.llen(key)) - count
                pipe.multi()
                pipe.ltrim(key, count, -1)
                if left <= 0:
                    pipe.delete(_buffer_ts_key(tenant_id, platform, channel_id))
                    pipe.srem(ACTIVE_BUFFERS_SET, _channel_ref(tenant_id, platform, channel_id))
                else:
                    pipe.set(_buffer_ts_key(tenant_id, platform, channel_id), str(_now()))
                await pipe.execute()
                return
            except WatchError:
                continue


async def get_active_channel_buffers(redis: Redis) -> list[tuple[UUID, SourcePlatform, str]]:
    """(tenant_id, platform, channel_id) for every non-empty buffer."""
    active: list[tuple[UUID, SourcePlatform, str]] = []
    for item in await redis.smembers(ACTIVE_BUFFERS_SET):
        parts = _decode(item).split(":", 2)
        if len(parts) != 3:
            continue
        try:
            active.append((UUID(parts[0]), SourcePlatform(parts[1]), parts[2]))
        except ValueError:
            continue
    return active


async def thread_tree(
    redis: Redis, tenant_id: UUID, platform: SourcePlatform, channel_id: str, thread_ts: str
) -> str | None:
    """The waiting discussion that holds this thread, if any."""
    raw = await redis.get(_thread_key(tenant_id, platform, channel_id, thread_ts))
    if raw is None:
        return None
    tree_id = _decode(raw)
    if await redis.zscore(ACTIVE_TREES_ZSET, _member(tenant_id, tree_id)) is None:
        return None
    return tree_id


async def map_thread(
    redis: Redis,
    tenant_id: UUID,
    platform: SourcePlatform,
    channel_id: str,
    thread_ts: str,
    tree_id: str,
) -> None:
    key = _thread_key(tenant_id, platform, channel_id, thread_ts)
    await redis.set(key, tree_id, ex=THREAD_TTL_SECONDS)


async def get_discourse_tree(redis: Redis, tenant_id: UUID, tree_id: str) -> dict[str, str]:
    data = await redis.hgetall(_tree_key(tenant_id, tree_id))
    return {_decode(k): _decode(v) for k, v in (data or {}).items()}


async def get_active_discourse_trees(
    redis: Redis, tenant_id: UUID, platform: SourcePlatform, channel_id: str
) -> list[dict[str, str]]:
    """Waiting discussions of one channel."""
    trees: list[dict[str, str]] = []
    for raw in await redis.smembers(_channel_trees_set(tenant_id, platform, channel_id)):
        tree = await get_discourse_tree(redis, tenant_id, _decode(raw))
        if tree:
            trees.append(tree)
    return trees


def _fields(
    tenant_id: UUID,
    tree_id: str,
    existing: dict[str, str],
    platform: SourcePlatform,
    channel_id: str,
    history: list[dict[str, Any]],
    audiences: list[dict[str, Any]],
    channel_name: str | None,
    connection: str | None,
    domain_tag: str,
    summary: str | None,
) -> dict[str, str]:
    now = datetime.now(UTC)
    return {
        "tree_id": tree_id,
        "tenant_id": str(tenant_id),
        "platform": platform.value,
        "channel_id": channel_id,
        "channel_name": channel_name or "",
        "connection": connection or "",
        "domain_tag": domain_tag,
        "summary": summary
        or existing.get("summary")
        or f"Discussion on {domain_tag} with {len(history)} messages.",
        "messages": json.dumps(history),
        "message_count": str(len(history)),
        "access_control": merge_channel_audience(channel_id, audiences).model_dump_json(),
        "created_ts": existing.get("created_ts") or str(now.timestamp()),
        "last_updated_ts": str(now.timestamp()),
        "last_updated_iso": now.isoformat(),
    }


async def create_or_update_discourse_tree(
    redis: Redis,
    tenant_id: UUID,
    tree_id: str,
    domain_tag: str,
    payloads: list[RawChatPayload],
    summary: str | None = None,
    *,
    idle_seconds: float | None = None,
    due_ts: float | None = None,
) -> int:
    """Add one channel's payloads to a discussion, skipping message ids it already holds, and
    make it due ``idle_seconds`` from now (or at ``due_ts``). Returns how many were added."""
    if not payloads:
        return 0
    platform, channel_id = payloads[0].platform, payloads[0].channel_id
    if any(p.platform != platform or p.channel_id != channel_id for p in payloads):
        raise ValueError("a discourse tree holds messages from one channel only")
    if idle_seconds is None and due_ts is None:
        from checkcord.core.config import get_settings

        idle_seconds = get_settings().chat_discussion_idle_seconds
    key = _tree_key(tenant_id, tree_id)
    while True:
        async with redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                raw = await pipe.hgetall(key)
                existing = {_decode(k): _decode(v) for k, v in (raw or {}).items()}
                if existing and (
                    existing.get("platform") != platform.value
                    or existing.get("channel_id") != channel_id
                ):
                    raise ValueError("discourse tree belongs to another channel")
                history: list[dict[str, Any]] = json.loads(existing.get("messages") or "[]")
                held = {m.get("message_id") for m in history}
                audiences: list[dict[str, Any]] = (
                    [json.loads(existing["access_control"])]
                    if existing.get("access_control")
                    else []
                )
                channel_name = existing.get("channel_name") or None
                connection = existing.get("connection") or None
                added = 0
                for p in payloads:
                    if p.message_id in held:
                        continue
                    held.add(p.message_id)
                    history.append(message_entry(p))
                    audiences.append(p.access_control.model_dump(mode="json"))
                    channel_name = p.channel_name or channel_name
                    connection = p.connection or connection
                    added += 1
                if not added:
                    await pipe.reset()
                    return 0
                history.sort(key=lambda m: m.get("happened_at") or "")
                fields = _fields(
                    tenant_id,
                    tree_id,
                    existing,
                    platform,
                    channel_id,
                    history,
                    audiences,
                    channel_name,
                    connection,
                    domain_tag,
                    summary,
                )
                due = due_ts if due_ts is not None else _now() + float(idle_seconds or 0)
                pipe.multi()
                pipe.hset(key, mapping=cast(Any, fields))
                pipe.hincrby(key, "version", 1)
                pipe.hdel(key, "outcomes")
                pipe.sadd(_channel_trees_set(tenant_id, platform, channel_id), tree_id)
                pipe.zadd(ACTIVE_TREES_ZSET, {_member(tenant_id, tree_id): due})
                await pipe.execute()
                break
            except WatchError:
                continue
    logger.info(
        "redis_trees.tree_updated",
        tenant_id=str(tenant_id),
        tree_id=tree_id,
        platform=platform.value,
        channel_id=channel_id,
        added=added,
        total_messages=len(history),
    )
    return added


async def remove_discourse_tree(redis: Redis, tenant_id: UUID, tree_id: str) -> dict[str, str]:
    """Delete a discussion everywhere and return what it held."""
    tree = await get_discourse_tree(redis, tenant_id, tree_id)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.delete(_tree_key(tenant_id, tree_id))
        pipe.delete(_lease_key(tenant_id, tree_id))
        pipe.zrem(ACTIVE_TREES_ZSET, _member(tenant_id, tree_id))
        pipe.zrem(FAILED_TREES_ZSET, _member(tenant_id, tree_id))
        if tree.get("platform") and tree.get("channel_id"):
            pipe.srem(_channel_trees_set(tenant_id, tree["platform"], tree["channel_id"]), tree_id)
        await pipe.execute()
    return tree


def parse_member(member: Any) -> tuple[UUID, str] | None:
    text = _decode(member)
    tenant, _, tree_id = text.partition(":")
    try:
        return UUID(tenant), tree_id
    except ValueError:
        return None


async def due_discussions(redis: Redis, now: float | None = None) -> list[tuple[UUID, str]]:
    found = await redis.zrangebyscore(ACTIVE_TREES_ZSET, min="-inf", max=now or _now())
    return [m for m in (parse_member(r) for r in found) if m is not None]


async def claim_discussion(
    redis: Redis, tenant_id: UUID, tree_id: str, lease: int = LEASE_SECONDS
) -> dict[str, str] | None:
    """Take a due discussion for ``lease`` seconds by moving its due time forward."""
    if not await redis.set(_lease_key(tenant_id, tree_id), "1", nx=True, ex=lease):
        return None
    member = _member(tenant_id, tree_id)
    if not await redis.zadd(ACTIVE_TREES_ZSET, {member: _now() + lease}, xx=True, ch=True):
        await redis.delete(_lease_key(tenant_id, tree_id))
        return None
    tree = await get_discourse_tree(redis, tenant_id, tree_id)
    if not tree:
        await redis.zrem(ACTIVE_TREES_ZSET, member)
        await redis.delete(_lease_key(tenant_id, tree_id))
        return None
    return tree


async def cache_outcomes(
    redis: Redis, tenant_id: UUID, tree_id: str, version: str, outcomes: list[dict[str, Any]]
) -> None:
    key = _tree_key(tenant_id, tree_id)
    while True:
        async with redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                if _decode(await pipe.hget(key, "version") or b"") != version:
                    await pipe.reset()
                    return
                pipe.multi()
                pipe.hset(key, "outcomes", json.dumps({"version": version, "items": outcomes}))
                await pipe.execute()
                return
            except WatchError:
                continue


def cached_outcomes(tree: dict[str, str]) -> list[dict[str, Any]] | None:
    raw = tree.get("outcomes")
    if not raw:
        return None
    data = json.loads(raw)
    if data.get("version") != tree.get("version"):
        return None
    return list(data.get("items") or [])


async def finish_discussion(
    redis: Redis, tenant_id: UUID, tree_id: str, version: str, done_ids: Iterable[str]
) -> bool:
    """Delete the extracted discussion. Messages that arrived meanwhile wait on as a new
    discussion. True when nothing was left over."""
    key = _tree_key(tenant_id, tree_id)
    member = _member(tenant_id, tree_id)
    done = set(done_ids)
    while True:
        async with redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                raw = await pipe.hgetall(key)
                tree = {_decode(k): _decode(v) for k, v in (raw or {}).items()}
                pipe.multi()
                pipe.delete(key)
                pipe.zrem(ACTIVE_TREES_ZSET, member)
                channel_set = (
                    _channel_trees_set(tenant_id, tree["platform"], tree["channel_id"])
                    if tree.get("platform") and tree.get("channel_id")
                    else None
                )
                if channel_set:
                    pipe.srem(channel_set, tree_id)
                left = [
                    m
                    for m in json.loads(tree.get("messages") or "[]")
                    if m.get("message_id") not in done
                ]
                deleted = not tree or tree.get("version") == version or not left
                if not deleted and channel_set:
                    from checkcord.core.config import get_settings

                    rest = str(uuid4())
                    kept = {
                        k: v
                        for k, v in tree.items()
                        if k not in ("outcomes", "attempts", "unusable", "last_error")
                    }
                    kept.update(
                        tree_id=rest, messages=json.dumps(left), message_count=str(len(left))
                    )
                    last = float(tree.get("last_updated_ts") or _now())
                    idle = get_settings().chat_discussion_idle_seconds
                    pipe.hset(_tree_key(tenant_id, rest), mapping=cast(Any, kept))
                    pipe.sadd(channel_set, rest)
                    pipe.zadd(ACTIVE_TREES_ZSET, {_member(tenant_id, rest): last + idle})
                    for thread in {m.get("thread_ts") or m.get("message_id") for m in left}:
                        if thread:
                            pipe.set(
                                _thread_key(
                                    tenant_id, tree["platform"], tree["channel_id"], thread
                                ),
                                rest,
                                ex=THREAD_TTL_SECONDS,
                            )
                pipe.delete(_lease_key(tenant_id, tree_id))
                await pipe.execute()
                return deleted
            except WatchError:
                continue


def retry_delay(attempts: int) -> float:
    return float(min(30 * 2 ** max(attempts - 1, 0), 900))


async def fail_discussion(
    redis: Redis, tenant_id: UUID, tree_id: str, error: str, *, unusable: bool
) -> bool:
    """Record a failed extraction and schedule the next one; park the discussion after
    ``UNUSABLE_LIMIT`` unusable replies. True when it was parked."""
    key = _tree_key(tenant_id, tree_id)
    member = _member(tenant_id, tree_id)
    now = _now()
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hincrby(key, "attempts", 1)
        pipe.hincrby(key, "unusable", 1 if unusable else 0)
        pipe.hset(key, mapping={"last_error": error[:ERROR_CHARS], "last_attempt_ts": str(now)})
        pipe.hget(key, "platform")
        pipe.hget(key, "channel_id")
        attempts, bad, _, platform, channel_id = await pipe.execute()
    parked = int(bad) >= UNUSABLE_LIMIT
    async with redis.pipeline(transaction=True) as pipe:
        if parked:
            pipe.zrem(ACTIVE_TREES_ZSET, member)
            pipe.zadd(FAILED_TREES_ZSET, {member: now})
            if platform and channel_id:
                pipe.srem(
                    _channel_trees_set(tenant_id, _decode(platform), _decode(channel_id)), tree_id
                )
        else:
            pipe.zadd(ACTIVE_TREES_ZSET, {member: now + retry_delay(int(attempts))}, xx=True)
        pipe.delete(_lease_key(tenant_id, tree_id))
        await pipe.execute()
    return parked


async def failed_discussions(redis: Redis, tenant_id: UUID) -> list[dict[str, str]]:
    trees = []
    for raw in await redis.zrange(FAILED_TREES_ZSET, 0, -1):
        found = parse_member(raw)
        if found is None or found[0] != tenant_id:
            continue
        tree = await get_discourse_tree(redis, tenant_id, found[1])
        if tree:
            trees.append(tree)
    return trees


async def retry_discussions(redis: Redis, tenant_id: UUID, tree_ids: Iterable[str] | None) -> int:
    """Put parked discussions (all of the tenant's when ``tree_ids`` is None) back, due now."""
    wanted = None if tree_ids is None else set(tree_ids)
    count = 0
    for tree in await failed_discussions(redis, tenant_id):
        tree_id = tree["tree_id"]
        if wanted is not None and tree_id not in wanted:
            continue
        member = _member(tenant_id, tree_id)
        async with redis.pipeline(transaction=True) as pipe:
            pipe.zrem(FAILED_TREES_ZSET, member)
            pipe.hdel(_tree_key(tenant_id, tree_id), "attempts", "unusable")
            pipe.sadd(_channel_trees_set(tenant_id, tree["platform"], tree["channel_id"]), tree_id)
            pipe.zadd(ACTIVE_TREES_ZSET, {member: _now()})
            await pipe.execute()
        count += 1
    return count


async def discussion_counts(redis: Redis, tenant_id: UUID | None = None) -> dict[str, Any]:
    """Waiting and parked discussions, with the age of the oldest waiting one and, for waiting
    discussions with a stored error, the longest-waiting error."""
    waiting, oldest, failed = 0, None, 0
    stuck: tuple[float, str] | None = None
    now = _now()
    for raw in await redis.zrange(ACTIVE_TREES_ZSET, 0, -1):
        found = parse_member(raw)
        if found is None or (tenant_id is not None and found[0] != tenant_id):
            continue
        waiting += 1
        tree = await get_discourse_tree(redis, found[0], found[1])
        created = float(tree.get("created_ts") or tree.get("last_updated_ts") or now)
        oldest = created if oldest is None else min(oldest, created)
        if tree.get("last_error") and (stuck is None or created < stuck[0]):
            stuck = (created, tree["last_error"])
    for raw in await redis.zrange(FAILED_TREES_ZSET, 0, -1):
        found = parse_member(raw)
        if found is not None and (tenant_id is None or found[0] == tenant_id):
            failed += 1
    return {
        "waiting": waiting,
        "oldest_minutes": round((now - oldest) / 60, 1) if oldest is not None else 0,
        "failed": failed,
        "error": stuck[1] if stuck else None,
        "error_minutes": round((now - stuck[0]) / 60, 1) if stuck else 0,
    }


async def buffered_counts(redis: Redis, tenant_id: UUID | None = None) -> dict[str, Any]:
    """Chat messages in channel buffers, not yet assigned to a discussion."""
    messages, oldest = 0, None
    for t, platform, channel_id in await get_active_channel_buffers(redis):
        if tenant_id is not None and t != tenant_id:
            continue
        n = await get_channel_buffer_length(redis, t, platform, channel_id)
        if not n:
            continue
        messages += n
        first = await get_channel_buffer_first_ts(redis, t, platform, channel_id)
        if first is not None:
            oldest = first if oldest is None else min(oldest, first)
    return {
        "buffered": messages,
        "buffered_oldest_minutes": round((_now() - oldest) / 60, 1) if oldest is not None else 0,
    }


async def no_outcome(redis: Redis, tenant_id: UUID) -> None:
    key = f"chat_no_outcome:{tenant_id}:{datetime.now(UTC).date().isoformat()}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, 400 * 24 * 3600)
        await pipe.execute()


async def not_settled(redis: Redis, tenant_id: UUID, count: int) -> None:
    key = f"chat_not_settled:{tenant_id}:{datetime.now(UTC).date().isoformat()}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incrby(key, count)
        pipe.expire(key, 400 * 24 * 3600)
        await pipe.execute()


async def not_settled_count(redis: Redis, tenant_id: UUID, days: Iterable[str]) -> int:
    keys = [f"chat_not_settled:{tenant_id}:{d}" for d in days]
    if not keys:
        return 0
    return sum(int(_decode(v)) for v in await redis.mget(keys) if v is not None)


async def no_outcome_count(redis: Redis, tenant_id: UUID, days: Iterable[str]) -> int:
    keys = [f"chat_no_outcome:{tenant_id}:{d}" for d in days]
    if not keys:
        return 0
    return sum(int(_decode(v)) for v in await redis.mget(keys) if v is not None)


async def update_message(redis: Redis, payload: RawChatPayload) -> bool:
    """Replace a waiting message's text in its buffer or discussion. True if it was found."""
    tenant_id, platform, channel_id = payload.tenant_id, payload.platform, payload.channel_id
    found = False
    key = _buffer_key(tenant_id, platform, channel_id)
    for index, raw in enumerate(await redis.lrange(key, 0, -1)):
        item = json.loads(_decode(raw))
        if item.get("message_id") == payload.message_id:
            item["content_raw"] = payload.content_raw
            item["content_clean"] = payload.content_clean
            await redis.lset(key, index, json.dumps(item))
            found = True
    for tree in await _channel_discussions(redis, tenant_id, platform, channel_id):
        messages = json.loads(tree.get("messages") or "[]")
        hit = False
        for m in messages:
            if m.get("message_id") == payload.message_id:
                m["content"] = payload.content_clean
                hit = True
        if hit:
            await _rewrite(redis, tenant_id, tree, messages)
            found = True
    return found


async def _channel_discussions(
    redis: Redis, tenant_id: UUID, platform: SourcePlatform | str, channel_id: str
) -> list[dict[str, str]]:
    trees = await get_active_discourse_trees(redis, tenant_id, SourcePlatform(platform), channel_id)
    seen = {t["tree_id"] for t in trees}
    for tree in await failed_discussions(redis, tenant_id):
        if (
            tree["tree_id"] not in seen
            and tree.get("platform") == SourcePlatform(platform).value
            and tree.get("channel_id") == channel_id
        ):
            trees.append(tree)
    return trees


async def _rewrite(
    redis: Redis, tenant_id: UUID, tree: dict[str, str], messages: list[dict[str, Any]]
) -> None:
    key = _tree_key(tenant_id, tree["tree_id"])
    if not messages:
        await remove_discourse_tree(redis, tenant_id, tree["tree_id"])
        return
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(
            key, mapping={"messages": json.dumps(messages), "message_count": str(len(messages))}
        )
        pipe.hincrby(key, "version", 1)
        pipe.hdel(key, "outcomes")
        await pipe.execute()


async def remove_messages(
    redis: Redis,
    tenant_id: UUID,
    platform: SourcePlatform | str,
    channel_id: str,
    message_ids: Iterable[str],
) -> int:
    """Take these messages out of the channel's buffer and discussions. Returns how many."""
    ids = set(message_ids)
    removed = 0
    key = _buffer_key(tenant_id, platform, channel_id)
    for raw in await redis.lrange(key, 0, -1):
        if json.loads(_decode(raw)).get("message_id") in ids:
            removed += int(await redis.lrem(key, 0, raw))
    if removed and not await redis.llen(key):
        await redis.delete(_buffer_ts_key(tenant_id, platform, channel_id))
        await redis.srem(ACTIVE_BUFFERS_SET, _channel_ref(tenant_id, platform, channel_id))
    for tree in await _channel_discussions(redis, tenant_id, platform, channel_id):
        messages = json.loads(tree.get("messages") or "[]")
        kept = [m for m in messages if m.get("message_id") not in ids]
        if len(kept) != len(messages):
            removed += len(messages) - len(kept)
            await _rewrite(redis, tenant_id, tree, kept)
    return removed


async def drop_chat_state(
    redis: Redis,
    tenant_id: UUID,
    *,
    channels: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset(),
    connection: str | None = None,
    authors: set[str] | frozenset[str] = frozenset(),
) -> int:
    """Delete the tenant's buffers and discussions of these (platform, channel) pairs or of this
    connection, and take these authors' messages out of the rest. Returns how many buffers and
    discussions changed."""

    def whole(platform: str, channel_id: str, conn: str | None) -> bool:
        return (platform, channel_id) in channels or (connection is not None and conn == connection)

    dropped = 0
    for t, platform, channel_id in await get_active_channel_buffers(redis):
        if t != tenant_id:
            continue
        key = _buffer_key(t, platform, channel_id)
        raws = await redis.lrange(key, 0, -1)
        items = [json.loads(_decode(i)) for i in raws]
        if (platform.value, channel_id) in channels or any(
            whole(platform.value, channel_id, i.get("connection")) for i in items
        ):
            async with redis.pipeline(transaction=True) as pipe:
                pipe.delete(key)
                pipe.delete(_buffer_ts_key(t, platform, channel_id))
                pipe.srem(ACTIVE_BUFFERS_SET, _channel_ref(t, platform, channel_id))
                await pipe.execute()
            dropped += 1
            continue
        theirs = [r for r, i in zip(raws, items, strict=True) if i.get("author_id") in authors]
        if theirs:
            for raw in theirs:
                await redis.lrem(key, 0, raw)
            if not await redis.llen(key):
                await redis.delete(_buffer_ts_key(t, platform, channel_id))
                await redis.srem(ACTIVE_BUFFERS_SET, _channel_ref(t, platform, channel_id))
            dropped += 1

    prefix = f"{tenant_id}:"
    members = [
        _decode(r)
        for zset in (ACTIVE_TREES_ZSET, FAILED_TREES_ZSET)
        for r in await redis.zrange(zset, 0, -1)
    ]
    for member in dict.fromkeys(members):
        if not member.startswith(prefix):
            continue
        tree_id = member[len(prefix) :]
        tree = await get_discourse_tree(redis, tenant_id, tree_id)
        if not tree or whole(
            tree.get("platform", ""), tree.get("channel_id", ""), tree.get("connection") or None
        ):
            await remove_discourse_tree(redis, tenant_id, tree_id)
            dropped += 1
            continue
        messages = json.loads(tree.get("messages") or "[]")
        kept = [m for m in messages if m.get("author_id") not in authors]
        if len(kept) != len(messages):
            await _rewrite(redis, tenant_id, tree, kept)
            dropped += 1
    return dropped
