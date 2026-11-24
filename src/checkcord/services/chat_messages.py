"""Stored chat messages and the discussions rebuilt from them when one is edited or removed."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

import structlog
from redis.asyncio import Redis
from sqlalchemy import String, cast, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.models.common import AccessControl, AuthorInfo, ChatMetadata
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.models.raw_payloads import RawChatPayload
from checkcord.storage import audit, redis_trees
from checkcord.storage.postgres import (
    PostgresEventStore,
    event_audience,
    events_table,
    tenant_context,
)

logger = structlog.get_logger(__name__)

_ev = events_table.c
_meta = events_table.c["metadata"]

ChatAction = Literal["created", "updated", "deleted"]
MessageRef = tuple[str, str, str]


def action(payload: RawChatPayload) -> ChatAction:
    event = payload.raw_event if isinstance(payload.raw_event, dict) else {}
    kind = event.get("subtype") or event.get("type")
    detail = (event.get("channelData") or {}).get("eventType")
    if kind in ("message_deleted", "messageDelete") or detail in (
        "deleteMessage",
        "messageDelete",
    ):
        return "deleted"
    if (
        kind in ("message_changed", "messageUpdate")
        or detail in ("editMessage", "messageEdit")
        or event.get("action") == "update"
    ):
        return "updated"
    return "created"


def is_reply(payload: RawChatPayload) -> bool:
    return bool(payload.thread_ts) and payload.thread_ts != payload.message_id


def source_id(channel_id: str, message_id: str) -> str:
    return f"chat-message:{channel_id}:{message_id}"


def message_event(payload: RawChatPayload) -> CanonicalEvent:
    return CanonicalEvent(
        tenant_id=payload.tenant_id,
        source_platform=payload.platform,
        source_type=SourceType.THREAD_REPLY if is_reply(payload) else SourceType.MESSAGE,
        source_id=source_id(payload.channel_id, payload.message_id),
        happened_at=payload.happened_at,
        author=AuthorInfo(
            source_id=payload.author_id or "unknown",
            email=payload.author_email,
            display_name=payload.author_name,
        ),
        content_raw=payload.content_raw,
        content_clean=payload.content_clean,
        access_control=payload.access_control,
        audience_container=payload.channel_id,
        connection=payload.connection,
        chat_metadata=ChatMetadata(channel_id=payload.channel_id, thread_id=payload.thread_ts),
        metadata={
            "chat_message": {
                "message_id": payload.message_id,
                "thread_ts": payload.thread_ts,
                "channel_id": payload.channel_id,
                "channel_name": payload.channel_name,
                "url": redis_trees.message_url(payload),
            }
        },
    )


async def store(engine: AsyncEngine, payload: RawChatPayload) -> bool:
    """Keep the message as an event; a second store of the same message is a no-op."""
    async with tenant_context(engine, payload.tenant_id, include_held=True) as conn:
        return await PostgresEventStore(conn).save_event(message_event(payload))


def _where(tenant_id: UUID, platform: str, channel_id: str, message_ids: Iterable[str]) -> Any:
    return (
        (_ev.tenant_id == tenant_id)
        & (_ev.source_platform == platform)
        & _ev.source_id.in_([source_id(channel_id, m) for m in message_ids])
    )


async def replace_text(engine: AsyncEngine, payload: RawChatPayload) -> bool:
    """Give the stored message its edited text. True if it was stored."""
    where = _where(
        payload.tenant_id, payload.platform.value, payload.channel_id, [payload.message_id]
    )
    async with tenant_context(engine, payload.tenant_id, include_held=True) as conn:
        result = await conn.execute(
            update(events_table)
            .where(where)
            .values(content_raw=payload.content_raw, content_clean=payload.content_clean)
        )
    return (result.rowcount or 0) > 0


async def stored_ids(
    engine: AsyncEngine, tenant_id: UUID, platform: str, channel_id: str, message_ids: list[str]
) -> list[UUID]:
    async with tenant_context(engine, tenant_id, include_held=True) as conn:
        found = await conn.execute(
            select(_ev.event_id).where(_where(tenant_id, platform, channel_id, message_ids))
        )
        return list(found.scalars())


async def _audit(payload: RawChatPayload, engine: AsyncEngine, name: str) -> None:
    async with tenant_context(engine, payload.tenant_id, include_held=True) as conn:
        await audit.record(
            conn,
            payload.tenant_id,
            audit.Actor(channel=audit.API),
            name,
            params={
                "platform": payload.platform.value,
                "container": payload.channel_id,
                "message_id": payload.message_id,
            },
        )


async def message_changed(engine: AsyncEngine, redis: Redis, payload: RawChatPayload) -> bool:
    """Apply an edit to the waiting and the stored copy and rebuild what was made from it.
    False when the message is not known, so it is handled as a new one."""
    waiting = await redis_trees.update_message(redis, payload)
    stored = await replace_text(engine, payload)
    if not (waiting or stored):
        return False
    await _audit(payload, engine, "source.message_changed")
    if stored:
        ref = (payload.platform.value, payload.channel_id, payload.message_id)
        await rebuild_discussions(
            engine,
            redis,
            payload.tenant_id,
            [ref],
            "message_changed",
            actor=audit.Actor(channel=audit.API),
            dropped=False,
        )
    return True


async def message_deleted(engine: AsyncEngine, redis: Redis, payload: RawChatPayload) -> None:
    """Remove the message everywhere; the purge rebuilds what was made from it."""
    from checkcord.services import purge

    await redis_trees.remove_messages(
        redis, payload.tenant_id, payload.platform, payload.channel_id, [payload.message_id]
    )
    ids = await stored_ids(
        engine, payload.tenant_id, payload.platform.value, payload.channel_id, [payload.message_id]
    )
    await _audit(payload, engine, "source.message_deleted")
    if ids:
        selection = purge.events(ids)
        selection = purge.Selection(
            selection.kind, {**selection.params, "reason": "message_deleted"}, selection.where
        )
        await purge.purge(
            engine,
            payload.tenant_id,
            selection,
            actor=audit.Actor(channel=audit.API),
            redis=redis,
        )


def _payload(tenant_id: UUID, row: Any) -> RawChatPayload:
    meta = (row.metadata or {}).get("chat_message") or {}
    author = row.author or {}
    url = meta.get("url")
    return RawChatPayload(
        tenant_id=tenant_id,
        platform=SourcePlatform(row.source_platform),
        channel_id=meta.get("channel_id") or row.audience_container,
        channel_name=meta.get("channel_name"),
        author_id=author.get("source_id"),
        author_email=author.get("email"),
        author_name=author.get("display_name"),
        message_id=meta["message_id"],
        thread_ts=meta.get("thread_ts"),
        happened_at=row.happened_at,
        content_raw=row.content_raw,
        content_clean=row.content_clean,
        raw_event={"webUrl": url} if url and row.source_platform != "slack" else {},
        access_control=AccessControl.model_validate(event_audience(row._mapping)),
        connection=row.connection,
    )


async def rebuild_discussions(
    engine: AsyncEngine,
    redis: Redis | None,
    tenant_id: UUID,
    messages: Iterable[MessageRef],
    reason: str,
    *,
    actor: audit.Actor,
    removed: Iterable[tuple[str, str, tuple[str, ...]]] = (),
    dropped: bool = True,
) -> int:
    """Remove, through purge, the outcome events of every discussion that held one of these
    messages, then queue each discussion's remaining stored messages as one discussion due
    now; with ``dropped=False`` (an edit) the messages themselves stay in it. ``removed`` lists
    discussions whose outcome events a purge already removed. Returns how many discussions were
    queued again."""
    from checkcord.services import purge

    by_channel: dict[tuple[str, str], set[str]] = {}
    for platform, channel_id, message_id in messages:
        by_channel.setdefault((platform, channel_id), set()).add(message_id)
    if not by_channel:
        return 0
    discussions: set[tuple[str, str, frozenset[str]]] = set()
    for platform, channel_id, held in removed:
        if set(held) & by_channel.get((platform, channel_id), set()):
            discussions.add((platform, channel_id, frozenset(held)))
    outcome_ids: list[UUID] = []
    async with tenant_context(engine, tenant_id, include_held=True, include_retired=True) as conn:
        for (platform, channel_id), ids in by_channel.items():
            chat = _meta["chat_source"]
            rows = await conn.execute(
                select(_ev.event_id, chat["discussion_message_ids"].label("ids")).where(
                    (_ev.tenant_id == tenant_id)
                    & (_ev.source_platform == platform)
                    & (chat["channel_id"].astext == channel_id)
                    & chat["discussion_message_ids"].has_any(cast(sorted(ids), ARRAY(String)))
                )
            )
            for row in rows:
                outcome_ids.append(row.event_id)
                discussions.add((platform, channel_id, frozenset(row.ids or ())))
    if outcome_ids:
        await purge.purge(
            engine,
            tenant_id,
            purge.Selection(
                "event",
                {"event_ids": [str(i) for i in outcome_ids], "reason": reason},
                _ev.event_id.in_(outcome_ids),
            ),
            actor=actor,
            redis=redis,
            rebuild=False,
            remember=False,
        )
    if redis is None:
        return 0
    queued = 0
    now = datetime.now(UTC).timestamp()
    for platform, channel_id, members in discussions:
        left = members - by_channel.get((platform, channel_id), set()) if dropped else members
        if not left:
            continue
        async with tenant_context(engine, tenant_id, include_held=True) as conn:
            rows = (
                await conn.execute(
                    select(
                        _ev.source_platform,
                        _ev.audience_container,
                        _ev.author,
                        _ev.happened_at,
                        _ev.content_raw,
                        _ev.content_clean,
                        _ev.access_control,
                        _ev.access_allowed_ids,
                        _ev.connection,
                        _meta.label("metadata"),
                    )
                    .where(_where(tenant_id, platform, channel_id, left))
                    .order_by(_ev.happened_at)
                )
            ).all()
        payloads = [_payload(tenant_id, r) for r in rows]
        if not payloads:
            continue
        await redis_trees.create_or_update_discourse_tree(
            redis, tenant_id, str(uuid4()), "HYBRID", payloads, due_ts=now
        )
        queued += 1
    logger.info(
        "chat.discussions_rebuilt",
        tenant_id=str(tenant_id),
        reason=reason,
        outcome_events=len(outcome_ids),
        queued=queued,
    )
    return queued
