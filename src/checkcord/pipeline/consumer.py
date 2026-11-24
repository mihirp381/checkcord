"""Saves raw events from the bus to Postgres."""

from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.bus.interface import EventBus
from checkcord.core import metrics
from checkcord.models.event import CanonicalEvent
from checkcord.pipeline.embedder import EmbeddingClient
from checkcord.storage import event_pipeline
from checkcord.storage.postgres import PostgresEventStore, list_tenant_ids, tenant_context

logger = structlog.get_logger(__name__)


class PipelineConsumer:
    """Saves events from the bus; messages in flight finish and are acknowledged on shutdown."""

    def __init__(
        self,
        bus: EventBus,
        engine: AsyncEngine,
        group_name: str = "pipeline_workers",
        consumer_name: str = "worker-1",
        embedder: EmbeddingClient | None = None,
    ) -> None:
        self.bus = bus
        self.engine = engine
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.stop_event = asyncio.Event()
        self.embedder = embedder

    async def process_message(self, event: CanonicalEvent) -> None:
        """Commit the event, then embed it; a failed embedding is retried from the row."""
        log = logger.bind(
            tenant_id=str(event.tenant_id),
            event_id=str(event.event_id),
        )

        try:
            if event.metadata.get("replaces") and not await self._replace(event):
                log.debug("pipeline.event_duplicate")
                return
            async with tenant_context(self.engine, event.tenant_id, include_held=True) as conn:
                store = PostgresEventStore(conn)

                inserted = await store.save_event(event, pipeline_pending=True)

        except Exception as e:
            log.exception("pipeline.process_error", error=str(e))
            # Re-raise so the EventBus DLQ mechanics handle retries/failures
            raise

        if not inserted:
            log.debug("pipeline.event_duplicate")
        else:
            metrics.EVENTS_INGESTED.labels(event.source_platform.value).inc()
            log.debug("pipeline.event_saved")

        await self.finish_event(event.tenant_id, event.event_id)

    async def _replace(self, event: CanonicalEvent) -> bool:
        """Delete earlier versions of an edited item; False when this version is stored."""
        from sqlalchemy import select

        from checkcord.services import purge
        from checkcord.storage import audit
        from checkcord.storage.postgres import events_table

        ev = events_table.c
        async with tenant_context(self.engine, event.tenant_id, include_held=True) as conn:
            rows = (
                await conn.execute(
                    select(ev.event_id, ev.fingerprint).where(
                        (ev.tenant_id == event.tenant_id)
                        & (ev.source_platform == event.source_platform.value)
                        & (ev.source_id == event.source_id)
                    )
                )
            ).all()
        if any(fingerprint == event.fingerprint for _, fingerprint in rows):
            return False
        if rows:
            await purge.purge(
                self.engine,
                event.tenant_id,
                purge.events([event_id for event_id, _ in rows]),
                actor=audit.Actor(channel=audit.API),
                audit_empty=False,
                remember=False,
            )
        return True

    async def finish_event(
        self, tenant_id: UUID, event_id: UUID, now: datetime | None = None
    ) -> bool:
        """Embed a saved, pending event and publish it to clustering. True if it finished now."""
        log = logger.bind(tenant_id=str(tenant_id), event_id=str(event_id))
        async with tenant_context(self.engine, tenant_id, include_held=True) as conn:
            claimed = await event_pipeline.claim(conn, tenant_id, event_id, now)
            if claimed is None:
                return False
            event = claimed.event
            try:
                embedding = (
                    await self.embedder.embed_text(event.content_clean) if self.embedder else None
                )
                if embedding:
                    embedded_event = event.model_copy(
                        update={"metadata": {**event.metadata, "embedding": embedding}}
                    )
                    await self.bus.publish("events.clustering.pending", embedded_event)
                    log.debug("pipeline.event_embedding_published")
            except Exception as err:
                state = await event_pipeline.mark_attempt_failed(conn, tenant_id, claimed, err, now)
                log.warning(
                    "pipeline.event_gave_up"
                    if state == event_pipeline.FAILED
                    else "pipeline.event_retry_scheduled",
                    attempts=claimed.attempts + 1,
                    error=event_pipeline.describe_error(err),
                )
                return False
            await event_pipeline.mark_done(conn, tenant_id, event_id)
            return True

    async def sweep_tenant(self, tenant_id: UUID, now: datetime | None = None) -> int:
        async with tenant_context(self.engine, tenant_id, include_held=True) as conn:
            due = await event_pipeline.due_event_ids(conn, tenant_id, now)
        finished = 0
        for event_id in due:
            if self.stop_event.is_set():
                break
            if await self.finish_event(tenant_id, event_id, now):
                finished += 1
        return finished

    async def sweep_unfinished(self) -> int:
        """Retry every due pending event, one tenant at a time."""
        finished = 0
        for tenant_id in await list_tenant_ids(self.engine):
            try:
                finished += await self.sweep_tenant(tenant_id)
            except Exception as e:
                logger.error("pipeline.sweep_failed", tenant_id=str(tenant_id), error=str(e))
        if finished:
            logger.info("pipeline.sweep_finished_events", count=finished)
        return finished

    async def _subscribe_to_topic(self, topic: str) -> None:
        """Background task that runs the infinite subscription loop."""
        try:
            # The EventBus subscribe method is a blocking infinite loop
            await self.bus.subscribe(
                topic=topic,
                group=self.group_name,
                consumer_name=self.consumer_name,
                handler=self.process_message,
            )
        except asyncio.CancelledError:
            logger.info("pipeline.subscription_cancelled", topic=topic)
        except Exception as e:
            logger.exception("pipeline.subscription_crashed", topic=topic, error=str(e))

    async def start(self, topics: list[str]) -> None:
        """Start consuming from the given topics and block until stopped."""
        logger.info("pipeline.starting", topics=topics, group=self.group_name)

        # Launch subscription tasks
        tasks = [asyncio.create_task(self._subscribe_to_topic(topic)) for topic in topics]

        # Wait until a graceful shutdown is triggered
        await self.stop_event.wait()
        logger.info("pipeline.shutdown_initiated")

        # Cancel all running subscription loops.
        # Note: The EventBus implementation should catch CancelledError and
        # allow any currently executing handler to finish before exiting.
        for t in tasks:
            t.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("pipeline.shutdown_complete")

    def stop(self) -> None:
        """Trigger a graceful shutdown of the consumer."""
        self.stop_event.set()
