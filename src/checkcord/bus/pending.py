"""Reading a stream as a consumer group, and taking back messages a crashed reader left pending."""

from __future__ import annotations

from typing import Any

import structlog
from redis.asyncio import Redis

logger = structlog.get_logger(__name__)


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _entry(entry: Any) -> tuple[str, dict[str, str]] | None:
    if not isinstance(entry, list | tuple) or len(entry) < 2 or not isinstance(entry[1], dict):
        return None
    return _text(entry[0]), {_text(k): _text(v) for k, v in entry[1].items()}


def entries(streams: Any) -> list[tuple[str, dict[str, str]]]:
    """The (message id, fields) pairs of an XREADGROUP reply."""
    out = []
    for stream in streams if isinstance(streams, list) else []:
        if isinstance(stream, list | tuple) and len(stream) >= 2 and isinstance(stream[1], list):
            out += [e for e in (_entry(m) for m in stream[1]) if e]
    return out


async def reclaim(
    redis: Redis, topic: str, group: str, consumer: str, *, min_idle_ms: int, count: int
) -> list[tuple[str, dict[str, str]]]:
    """Claim every message pending longer than min_idle_ms, from any consumer of the group."""
    cursor, out = "0-0", []
    while True:
        result = await redis.xautoclaim(
            name=topic,
            groupname=group,
            consumername=consumer,
            min_idle_time=min_idle_ms,
            start_id=cursor,
            count=count,
        )
        if not isinstance(result, list | tuple) or len(result) < 2:
            break
        out += [e for e in (_entry(m) for m in result[1] or []) if e]
        cursor = _text(result[0])
        if cursor == "0-0":
            break
    if out:
        logger.warning("stream.reclaimed", topic=topic, group=group, count=len(out))
    return out
