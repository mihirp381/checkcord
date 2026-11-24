"""Drops bot noise, then asks a model whether a message is about work."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from checkcord.adapters.chat.core.bot_filter import should_filter_message
from checkcord.bus.interface import EventBus
from checkcord.core.telemetry import increment_counter
from checkcord.llm.client import LLMClient
from checkcord.models.raw_payloads import RawChatPayload
from checkcord.services.chat_messages import action

logger = structlog.get_logger(__name__)


class PipelineScrubber:
    """Drops bot and noise chat messages and passes work messages on."""

    def __init__(
        self,
        bus: EventBus,
        llm_client: LLMClient,
        group_name: str = "scrubber_workers",
        consumer_name: str = "scrubber-1",
    ) -> None:
        self.bus = bus
        self.llm_client = llm_client
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.stop_event = asyncio.Event()

    async def process_message(self, raw_data: Any) -> None:
        """Process a single event payload from events.raw.chat."""
        try:
            if isinstance(raw_data, RawChatPayload):
                payload = raw_data
            elif isinstance(raw_data, dict):
                payload = RawChatPayload.model_validate(raw_data)
            else:
                payload = RawChatPayload.model_validate_json(raw_data)
        except Exception as e:
            logger.error("pipeline.scrubber_payload_validation_failed", error=str(e))
            increment_counter("chat_scrubber_error_total", tags={"error": "validation_error"})
            return

        bot_id = payload.raw_event.get("bot_id") if isinstance(payload.raw_event, dict) else None

        log = logger.bind(
            tenant_id=str(payload.tenant_id),
            message_id=payload.message_id,
            platform=payload.platform.value,
        )

        try:
            if action(payload) != "created":
                await self.bus.publish(topic="events.scrubbed.chat", event=payload)
                return

            # Drop bot and noise messages by pattern
            if should_filter_message(
                platform=payload.platform,
                bot_id=bot_id,
                username=payload.author_name,
                text=payload.content_clean,
            ):
                log.info("pipeline.scrubber_dropped", stage="stage0_bot_filter")
                increment_counter(
                    "chat_scrubber_dropped_total",
                    tags={"stage": "stage0_bot_filter", "platform": payload.platform.value},
                )
                return

            # Ask the model whether the message is about work
            is_work = await self.llm_client.classify_chat_message(payload.content_clean)
            if not is_work:
                log.info("pipeline.scrubber_dropped", stage="stage1_llm_scrubber")
                increment_counter(
                    "chat_scrubber_dropped_total",
                    tags={"stage": "stage1_llm_scrubber", "platform": payload.platform.value},
                )
                return

            # Message passed both stages -> Publish to events.scrubbed.chat
            msg_id = await self.bus.publish(topic="events.scrubbed.chat", event=payload)
            log.info("pipeline.scrubber_kept", topic="events.scrubbed.chat", msg_id=msg_id)
            increment_counter(
                "chat_scrubber_kept_total",
                tags={"platform": payload.platform.value},
            )

        except Exception as e:
            log.exception("pipeline.scrubber_processing_error", error=str(e))
            increment_counter("chat_scrubber_error_total", tags={"error": "processing_error"})
            raise

    async def _subscribe_to_topic(self, topic: str) -> None:
        """Background task running subscription loop."""
        try:
            await self.bus.subscribe(
                topic=topic,
                group=self.group_name,
                consumer_name=self.consumer_name,
                handler=self.process_message,
                payload_type=RawChatPayload,
            )
        except asyncio.CancelledError:
            logger.info("pipeline.scrubber_subscription_cancelled", topic=topic)
        except Exception as e:
            logger.exception("pipeline.scrubber_subscription_crashed", topic=topic, error=str(e))

    async def start(self, topics: list[str]) -> None:
        """Start consuming from topics and block until stopped."""
        logger.info("pipeline.scrubber_starting", topics=topics, group=self.group_name)
        tasks = [asyncio.create_task(self._subscribe_to_topic(topic)) for topic in topics]

        await self.stop_event.wait()
        logger.info("pipeline.scrubber_shutdown_initiated")

        for t in tasks:
            t.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("pipeline.scrubber_shutdown_complete")

    def stop(self) -> None:
        """Trigger graceful shutdown of the scrubber."""
        self.stop_event.set()
