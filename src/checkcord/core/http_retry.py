"""Bounded retry of provider calls that answer 429, honouring Retry-After."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

import structlog

from checkcord.core import metrics

if TYPE_CHECKING:
    import aiohttp
    import httpx
    from slack_sdk.web.async_client import AsyncWebClient

logger = structlog.get_logger(__name__)

MAX_ATTEMPTS = 4
MAX_WAIT_SECONDS = 60.0


def retry_after_seconds(value: str | None, attempt: int) -> float:
    """Seconds to wait: the header in seconds or as an HTTP date, else 2**attempt; capped."""
    wait = 2.0**attempt
    if value:
        try:
            wait = float(value)
        except ValueError:
            try:
                wait = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError):
                pass
    return max(0.0, min(wait, MAX_WAIT_SECONDS))


async def wait_or_give_up(provider: str, attempt: int, retry_after: str | None) -> bool:
    """After a 429 on ``attempt``: wait as asked and return True, or False once the bound is hit."""
    if attempt >= MAX_ATTEMPTS:
        logger.warning("provider_rate_limited.gave_up", provider=provider, attempts=attempt)
        return False
    wait = retry_after_seconds(retry_after, attempt)
    metrics.PROVIDER_RATE_LIMITED.labels(provider=provider).inc()
    logger.warning("provider_rate_limited", provider=provider, attempt=attempt, wait=wait)
    await asyncio.sleep(wait)
    return True


async def with_retry[T](
    call: Callable[[], Awaitable[T]],
    status: Callable[[T], tuple[int, str | None]],
    *,
    provider: str,
) -> T:
    """Repeat ``call`` while it answers 429, at most MAX_ATTEMPTS times; returns the last answer."""
    attempt = 0
    while True:
        attempt += 1
        result = await call()
        code, retry_after = status(result)
        if code != 429 or not await wait_or_give_up(provider, attempt, retry_after):
            return result


async def send(
    provider: str, client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    call = getattr(client, method.lower())
    return await with_retry(
        lambda: call(url, **kwargs),
        lambda r: (r.status_code, r.headers.get("Retry-After")),
        provider=provider,
    )


@dataclass(frozen=True)
class Answer:
    status: int
    headers: Mapping[str, str]
    body: Any
    text: str


async def aiohttp_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    provider: str,
    as_json: bool = True,
    **kwargs: Any,
) -> Answer:
    """One aiohttp call read in full; ``body`` is the JSON on a 2xx answer, else None."""

    request = getattr(session, method.lower())

    async def call() -> Answer:
        async with request(url, **kwargs) as resp:
            text = await resp.text()
            ok = as_json and 200 <= resp.status < 300 and text
            body = json.loads(text) if ok else None
            return Answer(resp.status, resp.headers.copy(), body, text)

    return await with_retry(
        call, lambda a: (a.status, a.headers.get("Retry-After")), provider=provider
    )


def bounded_slack[C: AsyncWebClient](client: C) -> C:
    """Let the Slack client wait out a 429 as Slack asks, a bounded number of times."""
    from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler

    client.retry_handlers.append(AsyncRateLimitErrorRetryHandler(max_retry_count=MAX_ATTEMPTS - 1))
    return client
