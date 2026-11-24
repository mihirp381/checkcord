"""Assigns kept chat messages to discussions: replies by thread, the rest by the model."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.bus.interface import EventBus
from checkcord.core.config import get_settings
from checkcord.core.telemetry import increment_counter
from checkcord.llm.client import LLMClient
from checkcord.models.enums import SourcePlatform
from checkcord.models.raw_payloads import RawChatPayload
from checkcord.services import chat_messages
from checkcord.storage import redis_trees

logger = structlog.get_logger(__name__)


class ChatRouter:
    def __init__(
        self,
        bus: EventBus,
        redis: Redis,
        llm_client: LLMClient,
        engine: AsyncEngine | None = None,
        group_name: str = "chat_router_workers",
        consumer_name: str = "chat-router-1",
        sweep_interval_seconds: float = 5.0,
    ) -> None:
        self.bus = bus
        self.redis = redis
        self.llm_client = llm_client
        self.engine = engine
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.sweep_interval_seconds = sweep_interval_seconds
        self.settings = get_settings()
        self.stop_event = asyncio.Event()

    async def process_message(self, raw_data: Any) -> None:
        try:
            if isinstance(raw_data, RawChatPayload):
                payload = raw_data
            elif isinstance(raw_data, dict):
                payload = RawChatPayload.model_validate(raw_data)
            else:
                payload = RawChatPayload.model_validate_json(raw_data)
        except Exception as e:
            logger.error("chat_router.payload_validation_failed", error=str(e))
            increment_counter("chat_router_error_total", tags={"error": "validation_error"})
            return

        kind = chat_messages.action(payload)
        if kind == "deleted":
            await self._deleted(payload)
            return
        if kind == "updated" and await self._changed(payload):
            return
        if self.engine is not None:
            await chat_messages.store(self.engine, payload)
        tenant_id, platform, channel_id = payload.tenant_id, payload.platform, payload.channel_id
        thread = payload.thread_ts if chat_messages.is_reply(payload) else payload.message_id
        if chat_messages.is_reply(payload) or await redis_trees.thread_tree(
            self.redis, tenant_id, platform, channel_id, payload.message_id
        ):
            await self._join_thread(payload, str(thread))
            return
        length = await redis_trees.push_to_channel_buffer(self.redis, payload)
        if length >= self.settings.chat_buffer_max_messages:
            await self.flush_buffer_block(tenant_id, platform, channel_id)

    async def _deleted(self, payload: RawChatPayload) -> None:
        if self.engine is None:
            await redis_trees.remove_messages(
                self.redis,
                payload.tenant_id,
                payload.platform,
                payload.channel_id,
                [payload.message_id],
            )
            return
        await chat_messages.message_deleted(self.engine, self.redis, payload)

    async def _changed(self, payload: RawChatPayload) -> bool:
        if self.engine is None:
            return await redis_trees.update_message(self.redis, payload)
        return await chat_messages.message_changed(self.engine, self.redis, payload)

    async def _join_thread(self, payload: RawChatPayload, thread: str) -> str:
        args = (self.redis, payload.tenant_id, payload.platform, payload.channel_id)
        tree_id = await redis_trees.thread_tree(*args, thread) or str(uuid4())
        await redis_trees.create_or_update_discourse_tree(
            self.redis, payload.tenant_id, tree_id, "HYBRID", [payload]
        )
        await redis_trees.map_thread(*args, thread, tree_id)
        increment_counter(
            "chat_router_thread_joined_total", tags={"platform": payload.platform.value}
        )
        return tree_id

    async def flush_buffer_block(
        self, tenant_id: UUID, platform: SourcePlatform, channel_id: str
    ) -> None:
        """Assign the channel's buffered messages, then take them out of the buffer."""
        if not await redis_trees.lock_channel_buffer(self.redis, tenant_id, platform, channel_id):
            return
        try:
            count, payloads = await redis_trees.read_channel_buffer(
                self.redis, tenant_id, platform, channel_id
            )
            rest: list[RawChatPayload] = []
            for p in payloads:
                if await redis_trees.thread_tree(
                    self.redis, tenant_id, platform, channel_id, p.message_id
                ):
                    await self._join_thread(p, p.message_id)
                else:
                    rest.append(p)
            if rest:
                await self._assign(tenant_id, platform, channel_id, rest)
            if count:
                await redis_trees.trim_channel_buffer(
                    self.redis, tenant_id, platform, channel_id, count
                )
        finally:
            await redis_trees.unlock_channel_buffer(self.redis, tenant_id, platform, channel_id)

    async def _assign(
        self,
        tenant_id: UUID,
        platform: SourcePlatform,
        channel_id: str,
        payloads: list[RawChatPayload],
    ) -> None:
        block = [
            {
                "message_id": p.message_id,
                "author": p.author_name or "unknown",
                "content": p.content_clean,
                "happened_at": p.happened_at.isoformat(),
            }
            for p in payloads
        ]
        trees = await redis_trees.get_active_discourse_trees(
            self.redis, tenant_id, platform, channel_id
        )
        routed = await self.llm_client.assign_discourse_tree(
            [
                {k: t.get(k) for k in ("tree_id", "domain_tag", "summary", "message_count")}
                for t in trees
            ],
            block,
        )
        tree_id = routed["tree_id"]
        if tree_id not in {t.get("tree_id") for t in trees}:
            tree_id = str(uuid4())
        await redis_trees.create_or_update_discourse_tree(
            self.redis,
            tenant_id,
            tree_id,
            routed["domain_tag"],
            payloads,
            summary=routed["summary"],
        )
        for p in payloads:
            await redis_trees.map_thread(
                self.redis, tenant_id, platform, channel_id, p.message_id, tree_id
            )
        increment_counter(
            "chat_router_block_routed_total",
            tags={"domain_tag": routed["domain_tag"], "platform": platform.value},
        )
        logger.info(
            "chat_router.block_routed",
            tenant_id=str(tenant_id),
            channel_id=channel_id,
            tree_id=tree_id,
            block_size=len(payloads),
        )

    async def sweep_once(self) -> None:
        now = datetime.now(UTC).timestamp()
        for tenant_id, platform, channel_id in await redis_trees.get_active_channel_buffers(
            self.redis
        ):
            first = await redis_trees.get_channel_buffer_first_ts(
                self.redis, tenant_id, platform, channel_id
            )
            if first is not None and now - first >= self.settings.chat_buffer_timeout_seconds:
                await self.flush_buffer_block(tenant_id, platform, channel_id)

    async def _sweeper_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await asyncio.sleep(self.sweep_interval_seconds)
                await self.sweep_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("chat_router.sweep_error", error=str(e))

    async def _subscribe_to_topic(self, topic: str) -> None:
        try:
            await self.bus.subscribe(
                topic=topic,
                group=self.group_name,
                consumer_name=self.consumer_name,
                handler=self.process_message,
                payload_type=RawChatPayload,
            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("chat_router.subscription_crashed", topic=topic, error=str(e))

    async def start(self, topics: list[str]) -> None:
        logger.info("chat_router.starting", topics=topics, group=self.group_name)
        tasks = [asyncio.create_task(self._subscribe_to_topic(t)) for t in topics]
        tasks.append(asyncio.create_task(self._sweeper_loop()))
        await self.stop_event.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def stop(self) -> None:
        self.stop_event.set()
