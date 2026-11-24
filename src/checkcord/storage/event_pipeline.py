"""Progress of saved events through the embedding step, tracked on the ``events`` row.

``pending`` rows are finished by the consumer or by the worker's sweep; a row is
claimed with ``FOR UPDATE SKIP LOCKED`` so only one worker works on it at a time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncConnection

from checkcord.core.secret_patterns import redact_secrets
from checkcord.models.event import CanonicalEvent
from checkcord.storage.postgres import PostgresEventStore, events_table

PENDING = "pending"
DONE = "done"
FAILED = "failed"

RETRY_WINDOW = timedelta(hours=24)
BACKOFF_BASE = timedelta(seconds=30)
BACKOFF_MAX = timedelta(hours=1)
OVERDUE_AFTER = timedelta(minutes=10)
ERROR_MAX_CHARS = 500

_c = events_table.c


def retry_delay(attempts: int) -> timedelta:
    delay: timedelta = BACKOFF_BASE * 2 ** min(max(attempts - 1, 0), 16)
    return min(delay, BACKOFF_MAX)


def describe_error(error: BaseException) -> str:
    text = redact_secrets(f"{type(error).__name__}: {error}")
    return text[:ERROR_MAX_CHARS]


@dataclass(frozen=True)
class ClaimedEvent:
    event: CanonicalEvent
    attempts: int
    started_at: datetime


@dataclass(frozen=True)
class UnfinishedEvent:
    event_id: UUID
    source: str
    ingested_at: datetime
    attempts: int
    last_error: str | None


async def claim(
    conn: AsyncConnection, tenant_id: UUID, event_id: UUID, now: datetime | None = None
) -> ClaimedEvent | None:
    """Lock a pending, due row for this transaction; None if done, failed, waiting or locked."""
    now = now or datetime.now(UTC)
    stmt = (
        select(events_table)
        .where(
            _c.tenant_id == tenant_id,
            _c.event_id == event_id,
            _c.pipeline_state == PENDING,
            _c.pipeline_next_attempt_at <= now,
        )
        .with_for_update(skip_locked=True)
    )
    row = (await conn.execute(stmt)).mappings().first()
    if row is None:
        return None
    data: dict[str, Any] = dict(row)
    return ClaimedEvent(
        event=PostgresEventStore._row_to_event(dict(data)),
        attempts=data["pipeline_attempts"],
        started_at=data["pipeline_started_at"] or now,
    )


async def mark_done(conn: AsyncConnection, tenant_id: UUID, event_id: UUID) -> None:
    await conn.execute(
        update(events_table)
        .where(_c.tenant_id == tenant_id, _c.event_id == event_id)
        .values(pipeline_state=DONE, pipeline_next_attempt_at=None, pipeline_last_error=None)
    )


async def mark_attempt_failed(
    conn: AsyncConnection,
    tenant_id: UUID,
    claimed: ClaimedEvent,
    error: BaseException,
    now: datetime | None = None,
) -> str:
    """Record a failed attempt; schedule the next one, or give up once the window has passed."""
    now = now or datetime.now(UTC)
    attempts = claimed.attempts + 1
    gave_up = now >= claimed.started_at + RETRY_WINDOW
    await conn.execute(
        update(events_table)
        .where(_c.tenant_id == tenant_id, _c.event_id == claimed.event.event_id)
        .values(
            pipeline_state=FAILED if gave_up else PENDING,
            pipeline_attempts=attempts,
            pipeline_next_attempt_at=None if gave_up else now + retry_delay(attempts),
            pipeline_last_error=describe_error(error),
        )
    )
    return FAILED if gave_up else PENDING


async def due_event_ids(
    conn: AsyncConnection, tenant_id: UUID, now: datetime | None = None, limit: int = 100
) -> list[UUID]:
    now = now or datetime.now(UTC)
    rows = await conn.execute(
        select(_c.event_id)
        .where(
            _c.tenant_id == tenant_id,
            _c.pipeline_state == PENDING,
            _c.pipeline_next_attempt_at <= now,
        )
        .order_by(_c.pipeline_next_attempt_at)
        .limit(limit)
    )
    return [r[0] for r in rows]


async def list_unfinished(
    conn: AsyncConnection,
    tenant_id: UUID,
    state: str = FAILED,
    limit: int = 100,
    after: tuple[datetime, UUID] | None = None,
) -> list[UnfinishedEvent]:
    stmt = select(
        _c.event_id,
        _c.source_platform,
        _c.source_type,
        _c.ingested_at,
        _c.pipeline_attempts,
        _c.pipeline_last_error,
    ).where(_c.tenant_id == tenant_id, _c.pipeline_state == state)
    if after is not None:
        stmt = stmt.where(tuple_(_c.ingested_at, _c.event_id) > tuple_(*after))
    rows = await conn.execute(stmt.order_by(_c.ingested_at, _c.event_id).limit(limit))
    return [
        UnfinishedEvent(
            event_id=r.event_id,
            source=f"{r.source_platform}/{r.source_type}",
            ingested_at=r.ingested_at,
            attempts=r.pipeline_attempts,
            last_error=r.pipeline_last_error,
        )
        for r in rows
    ]


async def count_unfinished(
    conn: AsyncConnection, tenant_id: UUID, now: datetime | None = None
) -> dict[str, int]:
    """Failed rows, and pending rows that failed at least once or that no sweep has picked up."""
    overdue = (now or datetime.now(UTC)) - OVERDUE_AFTER
    rows = await conn.execute(
        select(_c.pipeline_state, func.count())
        .where(
            _c.tenant_id == tenant_id,
            (_c.pipeline_state == FAILED)
            | (
                (_c.pipeline_state == PENDING)
                & ((_c.pipeline_attempts > 0) | (_c.pipeline_next_attempt_at < overdue))
            ),
        )
        .group_by(_c.pipeline_state)
    )
    counts = {FAILED: 0, PENDING: 0}
    counts.update({state: n for state, n in rows})
    return counts


async def retry_failed(
    conn: AsyncConnection,
    tenant_id: UUID,
    event_ids: list[UUID] | None = None,
    now: datetime | None = None,
) -> list[UUID]:
    """Put failed rows back in the sweep with a fresh retry window; all of them when no ids."""
    now = now or datetime.now(UTC)
    stmt = update(events_table).where(_c.tenant_id == tenant_id, _c.pipeline_state == FAILED)
    if event_ids is not None:
        stmt = stmt.where(_c.event_id.in_(event_ids))
    result = await conn.execute(
        stmt.values(
            pipeline_state=PENDING,
            pipeline_attempts=0,
            pipeline_started_at=now,
            pipeline_next_attempt_at=now,
        ).returning(_c.event_id)
    )
    return [r[0] for r in result]
