"""Events the pipeline could not finish: list them and put them back in the retry sweep."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.storage import audit, event_pipeline
from checkcord.storage.postgres import tenant_context


async def list_failed(
    engine: AsyncEngine,
    tenant_id: UUID,
    limit: int = 100,
    after: tuple[datetime, UUID] | None = None,
) -> tuple[list[event_pipeline.UnfinishedEvent], int]:
    """Failed events oldest first, and how many more are still being retried."""
    async with tenant_context(engine, tenant_id, include_held=True) as conn:
        failed = await event_pipeline.list_unfinished(conn, tenant_id, limit=limit, after=after)
        counts = await event_pipeline.count_unfinished(conn, tenant_id)
    return failed, counts[event_pipeline.PENDING]


async def retry(
    engine: AsyncEngine, tenant_id: UUID, event_ids: list[UUID] | None, actor: audit.Actor
) -> list[UUID]:
    """The ids queued again; all failed events when ``event_ids`` is None."""
    async with tenant_context(engine, tenant_id, include_held=True) as conn:
        retried = await event_pipeline.retry_failed(conn, tenant_id, event_ids)
        await audit.record(
            conn,
            tenant_id,
            actor,
            "events.retry",
            params={"all": event_ids is None},
            record_ids=retried,
            result_count=len(retried),
        )
    return retried
