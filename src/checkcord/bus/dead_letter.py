"""Dead-letter streams: writing, listing, replaying and dropping entries of ``<stream>.dlq``."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from checkcord.bus.token_scrub import scrub_tokens
from checkcord.core.secret_patterns import redact_secrets

DLQ_SUFFIX = ".dlq"
ERROR_FIELD = "dlq_error"
_PAGE = 100


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def dead_letter_fields(raw_payload: dict[str, str], error: str) -> dict[str, str]:
    """The original fields without stored credentials, plus the error that sent them here."""
    fields: dict[str, str] = scrub_tokens(raw_payload)
    fields.pop(ERROR_FIELD, None)
    fields[ERROR_FIELD] = redact_secrets(error)
    return fields


async def write_dead_letter(
    redis: Redis, topic: str, raw_payload: dict[str, str], error: str
) -> None:
    await redis.xadd(name=f"{topic}{DLQ_SUFFIX}", fields=dead_letter_fields(raw_payload, error))


def source_stream(dlq_stream: str) -> str:
    if not dlq_stream.endswith(DLQ_SUFFIX) or dlq_stream == DLQ_SUFFIX:
        raise ValueError(f"{dlq_stream!r} is not a dead-letter stream (name ends in {DLQ_SUFFIX})")
    return dlq_stream[: -len(DLQ_SUFFIX)]


def entry_age_seconds(entry_id: str, now: float | None = None) -> float:
    millis = int(entry_id.split("-", 1)[0])
    return max((now if now is not None else time.time()) - millis / 1000, 0.0)


_EXCEPTION_LINE = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt)\b:?")


def last_error_line(error: str) -> str:
    """The traceback's exception line, or the last line of anything else."""
    lines = [line.rstrip() for line in error.splitlines() if line.strip()]
    raised = [line for line in lines if _EXCEPTION_LINE.match(line)]
    return (raised or lines or [""])[-1].strip()


@dataclass(frozen=True)
class DeadLetterStream:
    name: str
    entries: int
    oldest_id: str


@dataclass(frozen=True)
class DeadLetter:
    entry_id: str
    fields: dict[str, str]

    @property
    def error(self) -> str:
        return self.fields.get(ERROR_FIELD, "")

    @property
    def original_fields(self) -> dict[str, str]:
        return {k: v for k, v in self.fields.items() if k != ERROR_FIELD}


async def list_streams(redis: Redis) -> list[DeadLetterStream]:
    streams = []
    async for key in redis.scan_iter(match=f"*{DLQ_SUFFIX}", _type="stream"):
        name = _text(key)
        first = await redis.xrange(name, count=1)
        if first:
            streams.append(DeadLetterStream(name, await redis.xlen(name), _text(first[0][0])))
    return sorted(streams, key=lambda s: s.name)


def _decode(entry: Any) -> DeadLetter:
    entry_id, fields = entry
    return DeadLetter(_text(entry_id), {_text(k): _text(v) for k, v in fields.items()})


async def entries(
    redis: Redis, stream: str, ids: list[str] | None = None, limit: int | None = None
) -> list[DeadLetter]:
    """Entries oldest first: the given ids that exist, or all of them up to ``limit``."""
    source_stream(stream)
    if ids is not None:
        found: list[DeadLetter] = []
        for entry_id in ids:
            found.extend(_decode(e) for e in await redis.xrange(stream, entry_id, entry_id))
        return found
    result: list[DeadLetter] = []
    start = "-"
    while limit is None or len(result) < limit:
        page = await redis.xrange(stream, start, "+", count=_PAGE)
        if not page:
            break
        result.extend(_decode(e) for e in page)
        start = "(" + _text(page[-1][0])
    return result if limit is None else result[:limit]


async def replay(redis: Redis, stream: str, ids: list[str] | None = None) -> list[str]:
    """Re-add each entry's original fields to its source stream, then delete it here."""
    target = source_stream(stream)
    replayed = []
    for letter in await entries(redis, stream, ids):
        await redis.xadd(target, letter.original_fields)
        await redis.xdel(stream, letter.entry_id)
        replayed.append(letter.entry_id)
    return replayed


async def drop(redis: Redis, stream: str, ids: list[str] | None = None) -> list[str]:
    dropped = [letter.entry_id for letter in await entries(redis, stream, ids)]
    if dropped:
        await redis.xdel(stream, *dropped)
    return dropped
