"""Trim Redis streams to the entries some consumer group still needs."""

from __future__ import annotations

from typing import Any

import structlog
from redis.asyncio import Redis

from checkcord.bus.dead_letter import DLQ_SUFFIX

logger = structlog.get_logger(__name__)


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _key(entry_id: str) -> tuple[int, int]:
    millis, _, seq = entry_id.partition("-")
    return int(millis), int(seq or 0)


def _successor(entry_id: str) -> str:
    millis, seq = _key(entry_id)
    return f"{millis}-{seq + 1}"


async def acknowledged_below(redis: Redis, stream: str) -> str | None:
    """Lowest entry id some group has not acknowledged yet; every entry below it is done.

    None when the stream has no consumer group, because then nobody has read it.
    """
    bounds = []
    for group in await redis.xinfo_groups(stream):
        info = {_text(k): v for k, v in group.items()}
        bound = _successor(_text(info["last-delivered-id"]))
        if int(info["pending"]):
            summary = await redis.xpending(stream, _text(info["name"]))
            if summary.get("min"):
                bound = min(bound, _text(summary["min"]), key=_key)
        bounds.append(bound)
    return min(bounds, key=_key) if bounds else None


async def trim_stream(redis: Redis, stream: str) -> int:
    bound = await acknowledged_below(redis, stream)
    if bound is None:
        return 0
    removed: int = await redis.xtrim(stream, minid=bound, approximate=False)
    return removed


async def trim_acknowledged(redis: Redis) -> dict[str, int]:
    """Trim every stream except dead-letter streams, which are emptied with `checkcord dlq`."""
    trimmed = {}
    async for key in redis.scan_iter(_type="stream"):
        stream = _text(key)
        if stream.endswith(DLQ_SUFFIX):
            continue
        try:
            removed = await trim_stream(redis, stream)
        except Exception as e:
            logger.warning("streams.trim_failed", stream=stream, error=str(e))
            continue
        if removed:
            trimmed[stream] = removed
    if trimmed:
        logger.info("streams.trimmed", removed=trimmed)
    return trimmed
