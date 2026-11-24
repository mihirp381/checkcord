"""What one tenant's Checkcord has done over recent days, from tables it already keeps."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.core.agent_text import MARKER
from checkcord.storage import audit, event_pipeline
from checkcord.storage.models import (
    AuditLogEntry,
    CanonicalContext,
    ContextEventLink,
    RemovedSource,
)
from checkcord.storage.postgres import DECISION_TYPES, events_table, tenant_context

logger = structlog.get_logger(__name__)

MESSAGE_PREFIX = "chat-message:"
OUTCOME_PREFIX = "chat-tree:"

_ev = events_table.c
_rec = CanonicalContext.__table__.c
_log = AuditLogEntry.__table__.c
_link = ContextEventLink.__table__.c


def _since(days: int) -> datetime:
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=days - 1)


async def _dead_letters(redis: Any, tenant_id: UUID) -> dict[str, int] | None:
    from checkcord.core.config import get_settings
    from checkcord.services.dead_letters import streams

    if redis is None:
        return None
    try:
        found = await streams(redis, tenant_id, get_settings().default_tenant_id)
        return {s.name: s.entries for s in found}
    except Exception as e:
        logger.warning("stats.dead_letters_unavailable", error=str(e))
        return None


async def _discussions(redis: Any, tenant_id: UUID, days: int) -> dict[str, Any] | None:
    from checkcord.storage import redis_trees

    if redis is None:
        return None
    try:
        counts = await redis_trees.discussion_counts(redis, tenant_id)
        buffered = await redis_trees.buffered_counts(redis, tenant_id)
        since = _since(days).date()
        window = [(since + timedelta(days=i)).isoformat() for i in range(days)]
        return {
            "waiting": counts["waiting"],
            "oldest_minutes": counts["oldest_minutes"],
            "failed": counts["failed"],
            **buffered,
            "no_outcome": await redis_trees.no_outcome_count(redis, tenant_id, window),
            "not_settled": await redis_trees.not_settled_count(redis, tenant_id, window),
        }
    except Exception as e:
        logger.warning("stats.discussions_unavailable", error=str(e))
        return None


async def summary(
    engine: AsyncEngine, tenant_id: UUID, days: int, actor: audit.Actor, redis: Any = None
) -> dict[str, Any]:
    from checkcord.services import scope
    from checkcord.services.connections import with_activity

    since = _since(days)
    async with tenant_context(engine, tenant_id, include_held=True, include_retired=True) as conn:
        day = cast(_ev.ingested_at, Date)
        message = _ev.source_id.startswith(MESSAGE_PREFIX)
        events = (
            await conn.execute(
                select(
                    _ev.source_platform,
                    day,
                    func.count().filter(message),
                    func.count().filter(~message),
                )
                .where(
                    (_ev.tenant_id == tenant_id)
                    & (_ev.ingested_at >= since)
                    & ~_ev.source_id.startswith(OUTCOME_PREFIX)
                )
                .group_by(_ev.source_platform, day)
                .order_by(day, _ev.source_platform)
            )
        ).all()
        held = _rec.held_connection.is_not(None)
        retired = _rec.retired_at.is_not(None)
        records = (
            await conn.execute(
                select(_rec.context_type, held, retired, func.count())
                .where(_rec.tenant_id == tenant_id)
                .group_by(_rec.context_type, held, retired)
                .order_by(_rec.context_type)
            )
        ).all()
        unfinished = await event_pipeline.count_unfinished(conn, tenant_id)
        marked = _rec.summary_redacted.contains(MARKER) | _rec.id.in_(
            select(_link.canonical_id).where(
                (_link.tenant_id == tenant_id) & _link.excerpt_redacted.contains(MARKER)
            )
        )
        with_removals = (
            await conn.execute(
                select(func.count()).where(
                    (_rec.tenant_id == tenant_id) & _rec.retired_at.is_(None) & marked
                )
            )
        ).scalar_one()

        read_day = cast(_log.created_at, Date)
        reads_window = (_log.tenant_id == tenant_id) & (_log.created_at >= since)
        is_read = _log.action.in_(audit.AGENT_READS)
        reads = (
            await conn.execute(
                select(read_day, func.count(), func.count(func.distinct(_log.user_id)))
                .where(reads_window & is_read)
                .group_by(read_day)
                .order_by(read_day)
            )
        ).all()
        readers = (
            await conn.execute(
                select(func.count(func.distinct(_log.user_id))).where(reads_window & is_read)
            )
        ).scalar_one()
        check = reads_window & (_log.action == audit.PR_CHECK)
        skipped_check = func.coalesce(_log.params["skipped"].as_boolean(), False)
        checks = (
            await conn.execute(
                select(
                    func.count().filter(~skipped_check),
                    func.count().filter(skipped_check),
                    func.count().filter(_log.params["flagged"].as_integer() > 0),
                    func.count().filter(_log.params["conflicts"].as_integer() > 0),
                    func.count().filter(_log.params["blocked"].as_boolean()),
                    func.count().filter(_log.params["error"].as_boolean()),
                ).where(check)
            )
        ).one()
        removed = (
            await conn.execute(
                select(func.count())
                .select_from(RemovedSource)
                .where(RemovedSource.tenant_id == tenant_id)
            )
        ).scalar_one()
        await audit.record(
            conn, tenant_id, actor, "stats.read", params={"days": days}, result_count=None
        )

    skipped = await scope.skipped(engine, tenant_id, days)
    connections = [
        {
            "connection": c["connection"],
            "state": c["state"],
            "last_event_at": c["last_event_at"].isoformat() if c["last_event_at"] else None,
            "scope_skipped": skipped.get(c["connection"], 0),
        }
        for c in await with_activity(engine, tenant_id)
    ]

    by_type: dict[str, dict[str, int]] = {}
    for context_type, is_held, is_retired, n in records:
        counts = by_type.setdefault(context_type or "general", {"live": 0, "held": 0, "retired": 0})
        counts["retired" if is_retired else "held" if is_held else "live"] += n

    return {
        "days": days,
        "since": since.date().isoformat(),
        "events": [
            {"source": source, "day": d.isoformat(), "messages": m, "events": n}
            for source, d, m, n in events
        ],
        "records": [
            {"type": t, "kind": "decision" if t in DECISION_TYPES else "reference", **c}
            for t, c in sorted(by_type.items())
        ],
        "records_with_removed_instructions": with_removals,
        "removed_remembered": removed,
        "events_unfinished": {
            "pending": unfinished[event_pipeline.PENDING],
            "failed": unfinished[event_pipeline.FAILED],
        },
        "dead_letters": await _dead_letters(redis, tenant_id),
        "discussions": await _discussions(redis, tenant_id, days),
        "agent_reads": [{"day": d.isoformat(), "reads": n, "people": p} for d, n, p in reads],
        "people_reading": readers,
        "pr_checks": dict(
            zip(
                ("posted", "skipped", "flagged", "conflicts", "blocked", "errors"),
                checks,
                strict=True,
            )
        ),
        "connections": connections,
        "scope_skipped": sum(skipped.values()),
    }
