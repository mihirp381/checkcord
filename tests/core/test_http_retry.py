from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler
from slack_sdk.web.async_client import AsyncWebClient

from checkcord.core import http_retry

pytestmark = pytest.mark.asyncio


def _limited_then(answers: list[httpx.Response]) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answers[min(len(seen), len(answers)) - 1]

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


async def test_retry_after_reads_seconds_and_dates_and_is_capped() -> None:
    assert http_retry.retry_after_seconds("7", 1) == 7
    soon = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    assert 25 <= http_retry.retry_after_seconds(soon, 1) <= 30
    assert http_retry.retry_after_seconds("3600", 1) == http_retry.MAX_WAIT_SECONDS
    assert http_retry.retry_after_seconds(None, 3) == 8
    assert http_retry.retry_after_seconds("soon", 2) == 4


async def test_send_waits_as_asked_then_returns_the_answer() -> None:
    client, seen = _limited_then(
        [
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(429, headers={"Retry-After": "5"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    with patch("asyncio.sleep", AsyncMock()) as sleep:
        resp = await http_retry.send("jira", client, "GET", "https://jira.example/issue")
    assert resp.status_code == 200
    assert len(seen) == 3
    assert [c.args[0] for c in sleep.await_args_list] == [3, 5]


async def test_send_gives_up_after_the_bound() -> None:
    client, seen = _limited_then([httpx.Response(429, headers={"Retry-After": "1"})])
    with patch("asyncio.sleep", AsyncMock()) as sleep:
        resp = await http_retry.send("linear", client, "POST", "https://api.linear.app/graphql")
    assert resp.status_code == 429
    assert len(seen) == http_retry.MAX_ATTEMPTS
    assert sleep.await_count == http_retry.MAX_ATTEMPTS - 1


async def test_other_failures_are_not_retried() -> None:
    client, seen = _limited_then([httpx.Response(503)])
    with patch("asyncio.sleep", AsyncMock()) as sleep:
        resp = await http_retry.send("notion", client, "GET", "https://api.notion.com/v1/x")
    assert resp.status_code == 503
    assert len(seen) == 1
    sleep.assert_not_awaited()


async def test_bounded_slack_adds_a_bounded_rate_limit_handler() -> None:
    client = http_retry.bounded_slack(AsyncWebClient(token="test-token"))
    handlers = [h for h in client.retry_handlers if isinstance(h, AsyncRateLimitErrorRetryHandler)]
    assert len(handlers) == 1
    assert handlers[0].max_retry_count == http_retry.MAX_ATTEMPTS - 1
