"""Weekly partition management for the ``events`` table."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

logger = structlog.get_logger(__name__)


async def _ensure_partition(
    conn: AsyncConnection, name: str, week_start: date, week_end: date
) -> None:
    # Runs as the schema owner (SECURITY DEFINER) so the application role can call it.
    moved = await conn.scalar(
        text("SELECT checkcord_ensure_events_partition(:name, :start, :end)"),
        {"name": name, "start": week_start, "end": week_end},
    )
    if moved:
        logger.info("partition.rows_moved_from_default", partition=name, rows=moved)


async def create_weekly_partitions(
    engine: AsyncEngine,
    weeks_ahead: int = 4,
) -> list[str]:
    """Create weekly range partitions for the ``events`` table.

    Generates partition tables named ``events_YYYY_wNN`` covering
    ``[monday, next_monday)`` for the current week plus *weeks_ahead* − 1
    future weeks.

    The operation is **idempotent**: partitions that already exist are
    silently skipped. Rows that landed in the default partition for a new
    week are moved into that week's partition.

    Parameters
    ----------
    engine:
        An async SQLAlchemy engine connected to the target database.
    weeks_ahead:
        How many weekly partitions to ensure exist (default ``4``).

    Returns
    -------
    list[str]
        Names of partition tables that were (or already) present.
    """
    today = datetime.now(UTC).date()
    # ISO weekday: Monday = 1, so offset = weekday() (Mon=0 in Python)
    current_monday = today - timedelta(days=today.weekday())

    created: list[str] = []

    async with engine.begin() as conn:
        for i in range(weeks_ahead):
            week_start = current_monday + timedelta(weeks=i)
            week_end = week_start + timedelta(weeks=1)

            iso_year, iso_week, _ = week_start.isocalendar()
            partition_name = f"events_{iso_year}_w{iso_week:02d}"

            await _ensure_partition(conn, partition_name, week_start, week_end)
            created.append(partition_name)

            logger.info(
                "partition.ensured",
                partition=partition_name,
                range_start=week_start.isoformat(),
                range_end=week_end.isoformat(),
            )

    return created


async def drop_empty_partitions(engine: AsyncEngine, before: date) -> list[str]:
    """Drop the weekly ``events`` partitions that end on or before *before* and hold no rows."""
    async with engine.begin() as conn:
        dropped = list(
            await conn.scalars(
                text("SELECT checkcord_drop_empty_events_partitions(:before)"), {"before": before}
            )
        )
    if dropped:
        logger.info("partition.dropped_empty", partitions=dropped)
    return dropped
