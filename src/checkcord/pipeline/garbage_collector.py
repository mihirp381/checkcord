"""Partition maintenance for the ``events`` table."""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.storage.partitions import create_weekly_partitions

logger = structlog.get_logger(__name__)


async def maintain_partitions(db_engine: AsyncEngine) -> None:
    """Create weekly range partitions for the ``events`` table."""
    logger.info("garbage_collector.maintain_partitions.started")
    try:
        await create_weekly_partitions(db_engine)
        logger.info("garbage_collector.maintain_partitions.completed")
    except Exception as e:
        logger.error("garbage_collector.maintain_partitions.failed", error=str(e))
        raise
