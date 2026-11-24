import asyncio
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.bus.interface import EventBus
from checkcord.models.event import CanonicalEvent
from checkcord.services.code_links import (
    extract_code_symbols,
    is_camel_case,
    is_snake_case,
    link_record,
)
from checkcord.storage.postgres import tenant_context

logger = structlog.get_logger(__name__)

__all__ = ["ASTLinkerWorker", "extract_code_symbols", "is_camel_case", "is_snake_case"]


class ASTLinkerWorker:
    """Links new records to the code symbols their summaries name."""

    def __init__(
        self,
        bus: EventBus,
        engine: AsyncEngine,
        group_name: str = "ast_linker_workers",
        consumer_name: str = "ast-linker-worker-1",
    ) -> None:
        self.bus = bus
        self.engine = engine
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.stop_event = asyncio.Event()

    async def process_message(self, event: CanonicalEvent) -> None:
        log = logger.bind(
            tenant_id=str(event.tenant_id),
            event_id=str(event.event_id),
            context_id=event.source_id,
        )

        log.info("ast_linker.processing_context_event")

        try:
            context_id = UUID(event.source_id)
        except ValueError:
            log.error("ast_linker.invalid_context_uuid", context_id_str=event.source_id)
            return
        if not extract_code_symbols(event.content_clean):
            log.info("ast_linker.no_symbols_found")
            return
        try:
            async with tenant_context(self.engine, event.tenant_id) as conn:
                made = await link_record(conn, event.tenant_id, context_id, event.content_clean)
            log.info("ast_linker.completed", links_created=made)
        except Exception as e:
            log.exception("ast_linker.operation_failed", error=str(e))
            raise

    async def _subscribe_to_topic(self, topic: str) -> None:
        """Background task running the Redis subscription loop."""
        try:
            await self.bus.subscribe(
                topic=topic,
                group=self.group_name,
                consumer_name=self.consumer_name,
                handler=self.process_message,
            )
        except asyncio.CancelledError:
            logger.info("ast_linker.subscription_cancelled", topic=topic)
        except Exception as e:
            logger.exception("ast_linker.subscription_crashed", topic=topic, error=str(e))

    async def start(self, topics: list[str]) -> None:
        """Starts the subscription and blocks until stopped."""
        logger.info("ast_linker.starting", topics=topics, group=self.group_name)

        tasks = [asyncio.create_task(self._subscribe_to_topic(topic)) for topic in topics]

        await self.stop_event.wait()
        logger.info("ast_linker.shutdown_initiated")

        for t in tasks:
            t.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("ast_linker.shutdown_complete")

    def stop(self) -> None:
        """Triggers graceful shutdown."""
        self.stop_event.set()
