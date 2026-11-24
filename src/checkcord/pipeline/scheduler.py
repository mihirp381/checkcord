from typing import Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.pipeline.garbage_collector import maintain_partitions
from checkcord.storage.scheduled_jobs import interval_gap, run_once

logger = structlog.get_logger(__name__)

CRON_GAP_SECONDS = 3600.0


def create_scheduler(db_engine: AsyncEngine, redis_client: Any = None) -> AsyncIOScheduler:
    """Create the API's scheduler. Every API process runs one; each job still runs once
    per interval in all, through ``run_once``. Only the usage flush runs in every process.
    """
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Shared event bus for jobs that publish. None without a redis_client;
    # jobs then skip publishing for that run.
    bus = None
    if redis_client is not None:
        from checkcord.bus.redis_streams import RedisStreamsEventBus

        bus = RedisStreamsEventBus(redis_client)

    def every(job_id: str, func: Any, minutes: float, *args: Any) -> None:
        scheduler.add_job(
            run_once,
            trigger="interval",
            minutes=minutes,
            args=[db_engine, job_id, interval_gap(minutes * 60), func, *args],
            id=job_id,
            replace_existing=True,
        )

    def cron(job_id: str, func: Any, *args: Any, **when: Any) -> None:
        scheduler.add_job(
            run_once,
            trigger="cron",
            args=[db_engine, job_id, CRON_GAP_SECONDS, func, *args],
            id=job_id,
            replace_existing=True,
            **when,
        )

    from checkcord.core.config import get_settings
    from checkcord.pipeline.aggregator import recalculate_confidence_scores
    from checkcord.services.audiences import refresh_due
    from checkcord.workers.judge_batch_worker import run_judge_batch_sweep
    from checkcord.workers.staleness_cron import run_staleness_pruning

    settings = get_settings()

    cron("maintain_partitions", maintain_partitions, db_engine, day_of_week="sun", hour=0, minute=0)
    every(
        "recalculate_confidence_scores",
        recalculate_confidence_scores,
        settings.confidence_aggregator_interval_minutes,
        db_engine,
    )
    # Staggered from maintain_partitions.
    cron("staleness_pruning", run_staleness_pruning, db_engine, day_of_week="sun", hour=2, minute=0)
    if settings.meeting_judge_mode == "batch":
        every(
            "judge_batch_sweep",
            run_judge_batch_sweep,
            settings.judge_batch_sweep_interval_minutes,
            db_engine,
            bus,
        )
    every(
        "refresh_audiences",
        refresh_due,
        settings.audience_refresh_interval_minutes,
        db_engine,
        redis_client,
    )

    from checkcord.llm.usage import flush as flush_usage

    # Flushes this process's own counts, so it is not shared.
    scheduler.add_job(
        flush_usage,
        trigger="interval",
        seconds=60,
        args=[db_engine],
        id="flush_model_usage",
        replace_existing=True,
    )

    if settings.event_retention_days is not None:
        from checkcord.services.purge import purge_expired

        cron("purge_expired_events", purge_expired, db_engine, redis_client, hour=3, minute=0)

    from checkcord import connectors as registered

    for c in registered.connectors():
        if c.poll is not None and (bus is not None or not c.poll.publishes):
            every(c.poll.job_id, c.poll.run, c.poll.minutes(settings), db_engine, bus)

    logger.info("scheduler.created", jobs=[job.id for job in scheduler.get_jobs()])
    return scheduler
