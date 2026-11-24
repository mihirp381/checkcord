"""Links entities across sources on events in the stream."""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.ingestion.cross_linker import extract_cross_links
from checkcord.bus.interface import EventBus
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent

logger = structlog.get_logger(__name__)


def get_enriched_topic(event: CanonicalEvent) -> str:
    """Determine the enriched topic based on event source type and platform."""
    if event.source_type == SourceType.CI_PIPELINE or event.source_platform in (
        SourcePlatform.DATADOG,
        SourcePlatform.SENTRY,
    ):
        return "events.enriched.monitoring"
    if event.source_platform == SourcePlatform.AGENT_SESSION:
        return "events.enriched.agent"
    return "events.enriched.chat"


class PipelineEnricher:
    """Adds cross-source links to raw events and publishes them to the enriched stream."""

    def __init__(
        self,
        bus: EventBus,
        engine: AsyncEngine,
        group_name: str = "enrichment_workers",
        consumer_name: str = "enricher-1",
    ) -> None:
        self.bus = bus
        self.engine = engine
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.stop_event = asyncio.Event()

    async def process_message(self, event: CanonicalEvent) -> None:
        """Enrich a single event from a raw stream and publish it to the enriched stream."""
        log = logger.bind(
            tenant_id=str(event.tenant_id),
            event_id=str(event.event_id),
        )

        try:
            # Extract cross-links using the database to filter Jira keys
            extracted_refs = await extract_cross_links(
                event.content_clean, event.tenant_id, self.engine
            )

            # Merge references while avoiding duplicates
            current_entities = event.entities or []
            existing_keys = {
                (ent.entity_type, ent.entity_id, ent.entity_platform) for ent in current_entities
            }

            merged_entities = list(current_entities)
            for ref in extracted_refs:
                key = (ref.entity_type, ref.entity_id, ref.entity_platform)
                if key not in existing_keys:
                    merged_entities.append(ref)
                    existing_keys.add(key)

            enriched_event = event.model_copy(update={"entities": merged_entities})

            enriched_topic = get_enriched_topic(enriched_event)
            msg_id = await self.bus.publish(topic=enriched_topic, event=enriched_event)

            log.debug(
                "pipeline.event_enriched",
                new_entities_count=len(extracted_refs),
                total_entities_count=len(merged_entities),
                enriched_topic=enriched_topic,
                msg_id=msg_id,
            )

        except Exception as e:
            log.exception("pipeline.enrichment_error", error=str(e))
            # Re-raise so the EventBus DLQ handles retries/failures
            raise

    async def _subscribe_to_topic(self, topic: str) -> None:
        """Background task that runs the infinite subscription loop."""
        try:
            await self.bus.subscribe(
                topic=topic,
                group=self.group_name,
                consumer_name=self.consumer_name,
                handler=self.process_message,
            )
        except asyncio.CancelledError:
            logger.info("pipeline.enricher_subscription_cancelled", topic=topic)
        except Exception as e:
            logger.exception("pipeline.enricher_subscription_crashed", topic=topic, error=str(e))

    async def start(self, topics: list[str]) -> None:
        """Start consuming from the given raw topics and block until stopped."""
        logger.info("pipeline.enricher_starting", topics=topics, group=self.group_name)

        tasks = [asyncio.create_task(self._subscribe_to_topic(topic)) for topic in topics]

        await self.stop_event.wait()
        logger.info("pipeline.enricher_shutdown_initiated")

        for t in tasks:
            t.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("pipeline.enricher_shutdown_complete")

    def stop(self) -> None:
        """Trigger a graceful shutdown of the enricher."""
        self.stop_event.set()
