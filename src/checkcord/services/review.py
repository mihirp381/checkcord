"""Connections in review: their events and records are held until an admin releases or rejects
them. Held rows carry the connection key in ``held_connection``; row-level security hides them
unless a transaction sets ``app.include_held``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy import func, select, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core import redaction
from checkcord.core.config import get_settings
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.pipeline.linkage import CONTEXTS_CREATED, context_created_event
from checkcord.services import purge
from checkcord.storage import audit
from checkcord.storage.models import CanonicalContext, ConnectionReview, ContextEventLink
from checkcord.storage.postgres import events_table, tenant_context

logger = structlog.get_logger(__name__).bind(component="review")

_reviews = ConnectionReview.__table__
_records = CanonicalContext.__table__
_links = ContextEventLink.__table__
_ev = events_table.c


@dataclass
class Released:
    events: int = 0
    records: list[dict[str, Any]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {"events": self.events, "records": len(self.records)}


def _state(tenant_id: UUID, key: str) -> Any:
    return (_reviews.c.tenant_id == tenant_id) & (_reviews.c.connection == key)


async def mark_new(conn: AsyncConnection, tenant_id: UUID, key: str, review: bool) -> None:
    """Record a connection's state when it is added: in review if asked, else the default.
    An existing connection keeps its state unless review is asked for."""
    stmt = pg_insert(_reviews).values(
        tenant_id=tenant_id,
        connection=key,
        in_review=review or get_settings().review_new_connections,
    )
    if review:
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "connection"],
            set_={"in_review": True, "updated_at": func.now()},
        )
    else:
        stmt = stmt.on_conflict_do_nothing()
    await conn.execute(stmt)


async def _set(conn: AsyncConnection, tenant_id: UUID, key: str, in_review: bool) -> None:
    stmt = pg_insert(_reviews).values(tenant_id=tenant_id, connection=key, in_review=in_review)
    await conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["tenant_id", "connection"],
            set_={"in_review": in_review, "updated_at": func.now()},
        )
    )


async def set_review(
    engine: AsyncEngine, tenant_id: UUID, key: str, in_review: bool, actor: audit.Actor
) -> None:
    """Turn review on or off. Material already held stays held until released or rejected."""
    async with tenant_context(engine, tenant_id) as conn:
        await _set(conn, tenant_id, key, in_review)
        await audit.record(
            conn,
            tenant_id,
            actor,
            "connection.review_changed",
            params={"connection": key, "in_review": in_review},
        )


async def states(conn: AsyncConnection, tenant_id: UUID) -> dict[str, bool]:
    rows = await conn.execute(
        select(_reviews.c.connection, _reviews.c.in_review).where(_reviews.c.tenant_id == tenant_id)
    )
    return {key: in_review for key, in_review in rows.all()}


async def held_counts(conn: AsyncConnection, tenant_id: UUID) -> dict[str, int]:
    """Held records per connection, for connections in review or with held records."""
    held = await conn.execute(
        select(_records.c.held_connection, func.count())
        .where((_records.c.tenant_id == tenant_id) & _records.c.held_connection.is_not(None))
        .group_by(_records.c.held_connection)
    )
    counts = {key: n for key, n in held.all()}
    for key, in_review in (await states(conn, tenant_id)).items():
        if in_review:
            counts.setdefault(key, 0)
    return counts


async def list_held(
    engine: AsyncEngine, tenant_id: UUID, actor: audit.Actor, connection: str | None = None
) -> list[dict[str, Any]]:
    """Held records, newest first, with redacted summaries, their sources and counts."""
    which = (_records.c.tenant_id == tenant_id) & _records.c.held_connection.is_not(None)
    if connection is not None:
        which = which & (_records.c.held_connection == connection)
    async with tenant_context(engine, tenant_id, include_held=True) as conn:
        rows = (
            await conn.execute(
                select(
                    _records.c.id,
                    _records.c.held_connection,
                    _records.c.context_type,
                    _records.c.severity,
                    _records.c.summary,
                    _records.c.summary_redacted,
                    _records.c.created_at,
                )
                .where(which)
                .order_by(_records.c.created_at.desc())
            )
        ).all()
        sources = await conn.execute(
            select(
                _links.c.canonical_id,
                _ev.source_platform,
                _ev.audience_container,
                func.count(),
            )
            .select_from(
                _links.join(
                    events_table,
                    (_ev.tenant_id == _links.c.tenant_id) & (_ev.event_id == _links.c.event_id),
                )
            )
            .where(
                (_links.c.tenant_id == tenant_id) & _links.c.canonical_id.in_([r.id for r in rows])
            )
            .group_by(_links.c.canonical_id, _ev.source_platform, _ev.audience_container)
        )
        by_record: dict[UUID, list[dict[str, Any]]] = {}
        for record_id, platform, container, n in sources.all():
            by_record.setdefault(record_id, []).append(
                {"platform": platform, "container": container, "events": n}
            )
        held = []
        redacted = 0
        for r in rows:
            summary = redaction.serve(r.summary, r.summary_redacted)
            redacted += summary != r.summary
            found = by_record.get(r.id, [])
            held.append(
                {
                    "id": str(r.id),
                    "connection": r.held_connection,
                    "context_type": r.context_type,
                    "severity": r.severity,
                    "summary": summary,
                    "created_at": r.created_at.isoformat(),
                    "sources": found,
                    "event_count": sum(s["events"] for s in found),
                }
            )
        await audit.record(
            conn,
            tenant_id,
            actor,
            "review.list",
            params={"connection": connection},
            record_ids=[r.id for r in rows],
            redacted=redacted,
            result_count=len(rows),
        )
    return held


async def _release(
    conn: AsyncConnection, tenant_id: UUID, key: str, events: Any, records: Any
) -> Released:
    released = Released()
    result = await conn.execute(
        update(events_table)
        .where((_ev.tenant_id == tenant_id) & (_ev.held_connection == key) & events)
        .values(held_connection=None)
    )
    released.events = result.rowcount or 0
    found = await conn.execute(
        update(_records)
        .where((_records.c.tenant_id == tenant_id) & (_records.c.held_connection == key) & records)
        .values(held_connection=None, updated_at=datetime.now(UTC))
        .returning(_records.c.id, _records.c.summary, _records.c.access_control)
    )
    released.records = [dict(r) for r in found.mappings().all()]
    return released


async def _announce(redis: Redis | None, tenant_id: UUID, released: Released) -> None:
    """Released records take the path of a new record: code links, then context files."""
    if redis is None or not released.records:
        return
    bus = RedisStreamsEventBus(redis)
    for r in released.records:
        await bus.publish(
            CONTEXTS_CREATED,
            context_created_event(
                tenant_id,
                r["id"],
                r["summary"],
                AuthorInfo(source_id="checkcord-review"),
                AccessControl.model_validate(r["access_control"]),
            ),
        )


async def release_connection(
    engine: AsyncEngine, tenant_id: UUID, key: str, actor: audit.Actor, redis: Redis | None = None
) -> Released:
    """Make the connection live and release everything held under it, in one transaction."""
    async with tenant_context(engine, tenant_id, include_held=True) as conn:
        await conn.execute(
            select(_reviews.c.connection).where(_state(tenant_id, key)).with_for_update()
        )
        await _set(conn, tenant_id, key, False)
        released = await _release(conn, tenant_id, key, true(), true())
        await audit.record(
            conn,
            tenant_id,
            actor,
            "review.released",
            params={"connection": key, **released.counts()},
            record_ids=[r["id"] for r in released.records],
            result_count=len(released.records),
        )
    await _announce(redis, tenant_id, released)
    logger.info("review.released", tenant_id=str(tenant_id), connection=key, **released.counts())
    return released


async def _held_record(conn: AsyncConnection, tenant_id: UUID, record_id: UUID) -> str | None:
    return await conn.scalar(
        select(_records.c.held_connection)
        .where((_records.c.tenant_id == tenant_id) & (_records.c.id == record_id))
        .with_for_update()
    )


async def release_record(
    engine: AsyncEngine,
    tenant_id: UUID,
    record_id: UUID,
    actor: audit.Actor,
    redis: Redis | None = None,
) -> Released | None:
    """Release one held record, with every held record that shares an event with it.
    None when the record is not held."""
    async with tenant_context(engine, tenant_id, include_held=True) as conn:
        key = await _held_record(conn, tenant_id, record_id)
        if key is None:
            return None
        records: set[UUID] = {record_id}
        events: set[UUID] = set()
        while True:
            found = await conn.execute(
                select(_links.c.event_id).where(
                    (_links.c.tenant_id == tenant_id) & _links.c.canonical_id.in_(records)
                )
            )
            more_events = set(found.scalars()) - events
            if not more_events:
                break
            events |= more_events
            found = await conn.execute(
                select(_links.c.canonical_id).where(
                    (_links.c.tenant_id == tenant_id) & _links.c.event_id.in_(events)
                )
            )
            records |= set(found.scalars())
        released = await _release(
            conn, tenant_id, key, _ev.event_id.in_(events), _records.c.id.in_(records)
        )
        await audit.record(
            conn,
            tenant_id,
            actor,
            "review.released",
            params={"connection": key, "record_id": str(record_id), **released.counts()},
            record_ids=[r["id"] for r in released.records],
            result_count=len(released.records),
        )
    await _announce(redis, tenant_id, released)
    return released


async def _record_rejection(
    engine: AsyncEngine,
    tenant_id: UUID,
    actor: audit.Actor,
    params: dict[str, Any],
    record_ids: list[UUID],
    purged: purge.Purged,
) -> None:
    async with tenant_context(engine, tenant_id) as conn:
        await audit.record(
            conn,
            tenant_id,
            actor,
            "review.rejected",
            params={**params, **purged.counts()},
            record_ids=record_ids,
            result_count=purged.records_deleted,
        )


async def reject_record(
    engine: AsyncEngine,
    tenant_id: UUID,
    record_id: UUID,
    actor: audit.Actor,
    redis: Redis | None = None,
) -> purge.Purged | None:
    """Delete a held record and the events linked only to it. None when it is not held."""
    async with tenant_context(engine, tenant_id, include_held=True) as conn:
        key = await _held_record(conn, tenant_id, record_id)
    if key is None:
        return None
    purged = await purge.purge(engine, tenant_id, purge.record(record_id), actor=actor, redis=redis)
    await _record_rejection(
        engine,
        tenant_id,
        actor,
        {"connection": key, "record_id": str(record_id)},
        [record_id],
        purged,
    )
    return purged


async def reject_connection(
    engine: AsyncEngine, tenant_id: UUID, key: str, actor: audit.Actor, redis: Redis | None = None
) -> purge.Purged:
    """Delete everything held under a connection. The connection stays in review."""
    async with tenant_context(engine, tenant_id, include_held=True) as conn:
        found = await conn.execute(
            select(_records.c.id).where(
                (_records.c.tenant_id == tenant_id) & (_records.c.held_connection == key)
            )
        )
        record_ids = list(found.scalars())
    selection = replace(purge.held(key), records=tuple(record_ids))
    purged = await purge.purge(engine, tenant_id, selection, actor=actor, redis=redis)
    await _record_rejection(engine, tenant_id, actor, {"connection": key}, record_ids, purged)
    return purged
