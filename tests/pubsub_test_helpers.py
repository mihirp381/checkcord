"""Shared helpers for real-bus producer/consumer tests.

A producer publishing shape X to a topic whose subscriber expects shape Y (or
that has no subscriber) is invisible to tests that mock `bus.subscribe` or
`bus.publish`, or call a worker's `.process_message()` directly. Use these
helpers when a test must prove that a real producer's output reaches a real
consumer's `subscribe()` call site.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any

from redis.asyncio import Redis


def _decode_fields(fields: dict[Any, Any]) -> dict[str, str]:
    return {
        (k.decode("utf-8") if isinstance(k, bytes) else str(k)): (
            v.decode("utf-8") if isinstance(v, bytes) else str(v)
        )
        for k, v in fields.items()
    }


async def wait_for_message_in_stream(
    redis_client: Redis,
    stream: str,
    predicate: Callable[[dict[str, str]], bool],
    timeout_seconds: float = 5.0,
    poll_interval: float = 0.1,
) -> dict[str, str] | None:
    """Poll `stream` until a message matching `predicate` appears, or return None.

    Typical predicate: `lambda fields: fields.get("event_id") == str(event_id)`.
    """
    elapsed = 0.0
    while elapsed < timeout_seconds:
        messages = await redis_client.xrange(stream)
        for _msg_id, fields in messages or []:
            if fields is None:
                continue
            decoded = _decode_fields(fields)
            if predicate(decoded):
                return decoded
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    return None


async def assert_topic_not_dlqd(redis_client: Redis, topic: str) -> None:
    """Assert no messages were routed to `{topic}.dlq` (i.e. nothing was a poison pill)."""
    dlq_topic = f"{topic}.dlq"
    dlq_messages = await redis_client.xrange(dlq_topic)
    assert not dlq_messages, (
        f"message(s) on {topic!r} were DLQ'd to {dlq_topic!r} as poison pills "
        f"instead of reaching the real consumer: {dlq_messages!r}"
    )


@asynccontextmanager
async def real_subscription(
    subscribe_coro_factory: Callable[[], Coroutine[Any, Any, None]],
) -> AsyncIterator[asyncio.Task[None]]:
    """Run a real (non-mocked) `bus.subscribe(...)`-based subscription loop for the
    duration of the `async with` block, then cancel it cleanly.

    `subscribe_coro_factory` is typically `lambda: worker._subscribe_to_topic(topic)`
    — the worker's own real subscription method, exercising its actual `payload_type=`
    wiring rather than a mocked bus.
    """
    task: asyncio.Task[None] = asyncio.create_task(subscribe_coro_factory())
    try:
        yield task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
