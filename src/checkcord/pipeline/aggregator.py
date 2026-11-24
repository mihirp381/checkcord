from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.core.config import get_settings
from checkcord.core.scoring import calculate_confidence_score
from checkcord.storage.postgres import PostgresEventStore, list_tenant_ids, tenant_context

logger = structlog.get_logger(__name__)


async def recalculate_confidence_scores(db_engine: AsyncEngine) -> None:
    """Recalculate confidence scores for contexts with new event links, one tenant at a time."""
    logger.info("confidence_aggregator.recalculate_confidence_scores.started")
    try:
        settings = get_settings()
        interval_mins = settings.confidence_aggregator_interval_minutes
        cutoff = datetime.now(UTC) - timedelta(minutes=interval_mins)

        stale_total = 0
        updated_total = 0
        for tenant_id in await list_tenant_ids(db_engine):
            try:
                async with tenant_context(db_engine, tenant_id) as conn:
                    store = PostgresEventStore(conn)
                    stale = await store.get_stale_contexts_global(cutoff)
                    context_ids = [cid for tid, cid in stale if tid == tenant_id]
                    stale_total += len(context_ids)

                    updates = []
                    for context_id in context_ids:
                        event_ids = await store.get_events_for_context(tenant_id, context_id)
                        events = await store.get_events_by_ids(tenant_id, event_ids)
                        updates.append((tenant_id, context_id, calculate_confidence_score(events)))
                    if updates:
                        updated_total += await store.update_context_scores_batch_global(updates)
            except Exception as tenant_ex:
                logger.error(
                    "confidence_aggregator.tenant_processing_failed",
                    tenant_id=str(tenant_id),
                    error=str(tenant_ex),
                )

        if not stale_total:
            logger.info("confidence_aggregator.recalculate_confidence_scores.no_stale_contexts")
            return
        logger.info(
            "confidence_aggregator.recalculate_confidence_scores.completed",
            stale_count=stale_total,
            updated_count=updated_total,
        )

    except Exception as e:
        logger.error("confidence_aggregator.recalculate_confidence_scores.failed", error=str(e))
        raise
