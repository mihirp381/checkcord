import asyncio
import traceback
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import ConnectionError, ResponseError, TimeoutError

from checkcord.bus.dead_letter import write_dead_letter
from checkcord.bus.interface import EventHandler
from checkcord.core import metrics
from checkcord.core.config import get_settings
from checkcord.llm import usage
from checkcord.models.event import CanonicalEvent

logger = structlog.get_logger(__name__)


class RedisStreamsEventBus:
    """The event bus on Redis Streams."""

    def __init__(self, redis_client: Redis) -> None:
        self.redis = redis_client
        self.max_retries = 3
        settings = get_settings()
        self.batch_size = settings.redis_stream_batch_size
        self.poll_timeout = settings.redis_stream_poll_timeout_ms
        self.reclaim_interval_ms = settings.redis_stream_reclaim_interval_ms
        self.reclaim_min_idle_ms = settings.redis_stream_reclaim_min_idle_ms

        self.circuit_breaker_open = False
        self.backoff_base = 1.0
        self.max_backoff = 30.0

    async def _ensure_group(self, topic: str, group: str) -> None:
        """Idempotently create a consumer group and stream."""
        try:
            # MKSTREAM creates the stream if it doesn't exist
            await self.redis.xgroup_create(name=topic, groupname=group, id="0", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" in str(e):
                pass  # Group already exists
            else:
                raise

    async def publish(self, topic: str, event: Any) -> str:
        """Publish an event to a Redis Stream."""
        import json

        if hasattr(event, "to_redis_dict"):
            payload = event.to_redis_dict()
        elif hasattr(event, "model_dump_json"):
            payload = {"data": event.model_dump_json()}
        elif isinstance(event, dict):
            payload = {"data": json.dumps(event)}
        else:
            payload = {"data": str(event)}

        # No MAXLEN: the worker trims only entries every consumer group has acknowledged.
        msg_id = await self.redis.xadd(name=topic, fields=payload)
        return msg_id.decode("utf-8") if isinstance(msg_id, bytes) else str(msg_id)

    async def acknowledge(self, topic: str, group: str, msg_id: str) -> None:
        """Acknowledge a message in the Redis Stream."""
        import typing

        xack: typing.Any = self.redis.xack
        await xack(topic, group, msg_id)

    @staticmethod
    def _decode_raw_payload(payload_data: dict[Any, Any]) -> dict[str, str]:
        """Normalize a redis-py message field dict (bytes or str keys/values) to str/str."""
        return {
            k.decode("utf-8") if isinstance(k, bytes) else str(k): v.decode("utf-8")
            if isinstance(v, bytes)
            else str(v)
            for k, v in payload_data.items()
        }

    async def subscribe(
        self,
        topic: str,
        group: str,
        consumer_name: str,
        handler: EventHandler,
        payload_type: type[Any] = CanonicalEvent,
    ) -> None:
        """
        Polls the Redis Stream infinitely.
        Processes messages sequentially. Handles DLQ logic.
        """
        await self._ensure_group(topic, group)
        logger.info("bus.subscribed", topic=topic, group=group, consumer=consumer_name)

        reclaim_task = asyncio.create_task(
            self._reclaim_loop(topic, group, consumer_name, handler, payload_type)
        )
        try:
            while True:
                try:
                    # Block for up to `poll_timeout` waiting for new messages
                    streams = await self.redis.xreadgroup(
                        groupname=group,
                        consumername=consumer_name,
                        streams={topic: ">"},
                        count=self.batch_size,
                        block=self.poll_timeout,
                    )

                    if self.circuit_breaker_open:
                        logger.info("bus.redis_recovered", topic=topic)
                        self.circuit_breaker_open = False
                        self.backoff_base = 1.0

                    if not streams or not isinstance(streams, list):
                        continue

                    for stream_data in streams:
                        if not isinstance(stream_data, list | tuple) or len(stream_data) < 2:
                            continue
                        stream_name = stream_data[0]
                        messages = stream_data[1]
                        stream_topic = (
                            stream_name.decode("utf-8")
                            if isinstance(stream_name, bytes)
                            else str(stream_name)
                        )

                        if not isinstance(messages, list):
                            continue

                        for message in messages:
                            if not isinstance(message, list | tuple) or len(message) < 2:
                                continue
                            msg_id_bytes = message[0]
                            payload_data = message[1]

                            if not isinstance(payload_data, dict):
                                continue

                            msg_id = (
                                msg_id_bytes.decode("utf-8")
                                if isinstance(msg_id_bytes, bytes)
                                else str(msg_id_bytes)
                            )

                            raw_payload = self._decode_raw_payload(payload_data)

                            await self._process_message(
                                stream_topic, group, msg_id, raw_payload, handler, payload_type
                            )

                except asyncio.CancelledError:
                    logger.info("bus.subscriber_cancelled", topic=topic)
                    break
                except (ConnectionError, TimeoutError) as e:
                    self.circuit_breaker_open = True
                    logger.warning(
                        "bus.redis_dropped",
                        error=str(e),
                        backoff=self.backoff_base,
                        topic=topic,
                    )
                    await asyncio.sleep(self.backoff_base)
                    self.backoff_base = min(self.backoff_base * 2, self.max_backoff)
                except Exception as e:
                    logger.exception("bus.subscriber_loop_error", error=str(e), topic=topic)
                    await asyncio.sleep(1)  # Backoff on critical Redis connection failures
        finally:
            reclaim_task.cancel()
            try:
                await reclaim_task
            except asyncio.CancelledError:
                pass

    async def _reclaim_loop(
        self,
        topic: str,
        group: str,
        consumer_name: str,
        handler: EventHandler,
        payload_type: type[Any],
    ) -> None:
        """Periodically reclaim pending messages idle longer than reclaim_min_idle_ms.

        Runs on its own timer (reclaim_interval_ms), independent of the
        XREADGROUP poll loop above: that loop only ever reads new messages
        (id='>'), so a message that fails once without exceeding max_retries
        would otherwise never be redelivered to anyone, even from a live
        consumer whose handler hit a transient error. This sweep is what
        gives every pending message a second (and subsequent) chance,
        feeding reclaimed messages through the same _process_message path
        -- same retry counter (keyed by msg_id, unaffected by reclaim),
        same max-retries-to-DLQ behavior -- as a freshly-polled message.
        """
        while True:
            try:
                await asyncio.sleep(self.reclaim_interval_ms / 1000)
                await self._reclaim_once(topic, group, consumer_name, handler, payload_type)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("bus.reclaim_loop_error", error=str(e), topic=topic)

    async def _reclaim_once(
        self,
        topic: str,
        group: str,
        consumer_name: str,
        handler: EventHandler,
        payload_type: type[Any],
    ) -> None:
        """Run XAUTOCLAIM to completion (following its cursor) for one sweep."""
        cursor = "0-0"
        while True:
            result = await self.redis.xautoclaim(
                name=topic,
                groupname=group,
                consumername=consumer_name,
                min_idle_time=self.reclaim_min_idle_ms,
                start_id=cursor,
                count=self.batch_size,
            )
            if not isinstance(result, list | tuple) or len(result) < 2:
                return

            next_cursor_raw, claimed = result[0], result[1]
            if isinstance(claimed, list) and claimed:
                logger.warning(
                    "bus.reclaiming",
                    topic=topic,
                    group=group,
                    consumer=consumer_name,
                    reclaimed_count=len(claimed),
                    min_idle_ms=self.reclaim_min_idle_ms,
                )
                for entry in claimed:
                    if not isinstance(entry, list | tuple) or len(entry) < 2:
                        continue
                    msg_id_bytes, payload_data = entry[0], entry[1]
                    if not isinstance(payload_data, dict):
                        continue
                    msg_id = (
                        msg_id_bytes.decode("utf-8")
                        if isinstance(msg_id_bytes, bytes)
                        else str(msg_id_bytes)
                    )
                    raw_payload = self._decode_raw_payload(payload_data)
                    await self._process_message(
                        topic, group, msg_id, raw_payload, handler, payload_type
                    )

            next_cursor = (
                next_cursor_raw.decode("utf-8")
                if isinstance(next_cursor_raw, bytes)
                else str(next_cursor_raw)
            )
            if next_cursor == "0-0" or not claimed:
                return
            cursor = next_cursor

    async def _process_message(
        self,
        topic: str,
        group: str,
        msg_id: str,
        raw_payload: dict[str, str],
        handler: EventHandler,
        payload_type: type[Any] = CanonicalEvent,
    ) -> None:
        """Process a single message, handle parsing, execution, and DLQ logic."""
        retry_key = f"retries:{topic}:{group}:{msg_id}"

        try:
            # Mirror publish()'s duck-typing: payload types with their own flattened
            # to_redis_dict/from_redis_dict (CanonicalEvent, RawChatPayload) must be
            # reconstructed via that method; everything else was published under the
            # generic {"data": model_dump_json()} fallback.
            if hasattr(payload_type, "from_redis_dict"):
                event: Any = payload_type.from_redis_dict(raw_payload)
            else:
                event = payload_type.model_validate_json(raw_payload["data"])
        except Exception as e:
            logger.error("bus.unparseable_message", error=str(e), msg_id=msg_id, topic=topic)
            await self._handle_dlq(topic, group, msg_id, raw_payload, traceback.format_exc())
            return

        try:
            usage.set_tenant(getattr(event, "tenant_id", None))
            # Execute handler sequentially for strict ordering and safety
            await handler(event)
            # Success: ACK and cleanup retry counters
            await self.acknowledge(topic, group, msg_id)
            await self.redis.delete(retry_key)
            metrics.STREAM_MESSAGES.labels(topic, group, "ok").inc()
        except Exception as e:
            # Failure: Increment retries atomically
            retries = await self.redis.incr(retry_key)
            metrics.STREAM_MESSAGES.labels(topic, group, "failed").inc()
            if retries > self.max_retries:
                logger.error("bus.dead_lettered", msg_id=msg_id, topic=topic)
                error_trace = traceback.format_exc()
                await self._handle_dlq(topic, group, msg_id, raw_payload, error_trace)
                await self.redis.delete(retry_key)
            else:
                logger.warning(
                    "bus.handler_failed",
                    msg_id=msg_id,
                    retries=retries,
                    error=str(e),
                )
                # Do NOT ack. It stays in the Pending Entries List (PEL).

    async def _handle_dlq(
        self, topic: str, group: str, msg_id: str, raw_payload: dict[str, str], error_msg: str
    ) -> None:
        """Push failed message to DLQ and ACK original stream."""
        await write_dead_letter(self.redis, topic, raw_payload, error_msg)
        metrics.STREAM_MESSAGES.labels(topic, group, "dead_letter").inc()
        await self.acknowledge(topic, group, msg_id)
