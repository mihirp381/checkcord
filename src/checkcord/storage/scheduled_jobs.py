"""Run each scheduled job at most once per interval across every process that schedules it.

A job takes a session advisory lock, so two runs never overlap, and claims its row in
``scheduled_jobs`` only if the last start is at least ``min_gap`` ago. Postgres drops the
lock when the holder's connection closes, so a process that dies does not block the next run.
"""

from __future__ import annotations

import zlib
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

logger = structlog.get_logger(__name__)

LOCK_NAMESPACE = 0x1EA5

_CLAIM = text(
    "INSERT INTO scheduled_jobs (job_id, last_started_at) VALUES (:job, now()) "
    "ON CONFLICT (job_id) DO UPDATE SET last_started_at = now() "
    "WHERE scheduled_jobs.last_started_at IS NULL "
    "OR scheduled_jobs.last_started_at <= now() - make_interval(secs => :gap) "
    "RETURNING job_id"
)
_FINISHED = {
    True: text("UPDATE scheduled_jobs SET last_succeeded_at = now() WHERE job_id = :job"),
    False: text("UPDATE scheduled_jobs SET last_failed_at = now() WHERE job_id = :job"),
}


def lock_key(job_id: str) -> int:
    key = zlib.crc32(job_id.encode())
    return key - 2**32 if key >= 2**31 else key


def interval_gap(seconds: float) -> float:
    """Gap between runs of an interval job; slightly short so tick jitter does not skip one."""
    return seconds - min(seconds * 0.1, 60.0)


async def _unlock(conn: AsyncConnection, job_id: str) -> None:
    try:
        await conn.execute(
            text("SELECT pg_advisory_unlock(:ns, :key)"),
            {"ns": LOCK_NAMESPACE, "key": lock_key(job_id)},
        )
        await conn.commit()
    except Exception as err:
        logger.warning("scheduled_job.unlock_failed", job=job_id, error=str(err))
        await conn.invalidate()


async def run_once(
    engine: AsyncEngine,
    job_id: str,
    min_gap_seconds: float,
    func: Callable[..., Awaitable[Any]],
    *args: Any,
) -> bool:
    """Run ``func(*args)`` if it is due and not running elsewhere. True if it ran and succeeded."""
    async with engine.connect() as lock_conn:
        locked = await lock_conn.scalar(
            text("SELECT pg_try_advisory_lock(:ns, :key)"),
            {"ns": LOCK_NAMESPACE, "key": lock_key(job_id)},
        )
        await lock_conn.commit()
        if not locked:
            logger.info("scheduled_job.skipped", job=job_id, reason="running_elsewhere")
            return False
        try:
            async with engine.begin() as conn:
                claimed = await conn.scalar(_CLAIM, {"job": job_id, "gap": min_gap_seconds})
            if claimed is None:
                logger.debug("scheduled_job.skipped", job=job_id, reason="not_due")
                return False
            logger.info("scheduled_job.started", job=job_id)
            ok = True
            try:
                await func(*args)
            except Exception as err:
                ok = False
                logger.exception("scheduled_job.failed", job=job_id, error=str(err))
            async with engine.begin() as conn:
                await conn.execute(_FINISHED[ok], {"job": job_id})
            logger.info("scheduled_job.finished", job=job_id, ok=ok)
            return ok
        finally:
            await _unlock(lock_conn, job_id)


async def last_runs(conn: AsyncConnection) -> dict[str, tuple[datetime | None, datetime | None]]:
    """Each job's last success and last failure."""
    rows = await conn.execute(
        text("SELECT job_id, last_succeeded_at, last_failed_at FROM scheduled_jobs")
    )
    return {row.job_id: (row.last_succeeded_at, row.last_failed_at) for row in rows}
