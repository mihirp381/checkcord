"""Request limits: fixed one-minute windows counted in Redis, shared by every API process.

When Redis cannot be reached, limits on authenticated callers are skipped (fail open), while
the failed-authentication and sign-in limits fall back to a count in this process (fail closed).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

from checkcord.core import metrics
from checkcord.core.config import get_settings

if TYPE_CHECKING:
    from checkcord.api.deps import Principal

logger = structlog.get_logger(__name__)

WINDOW_SECONDS = 60
PREFIX = "ratelimit:"
AUTH_FAILURES, SIGN_IN, PRINCIPAL, WEBHOOK = "auth_failures", "sign_in", "principal", "webhook"

_local_window = 0
_local_counts: dict[str, int] = {}


def reset_local() -> None:
    _local_counts.clear()


def client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _redis(request: Request) -> Any:
    ctx = getattr(request.app.state, "ctx", None)
    return getattr(ctx, "redis_client", None)


def _window() -> tuple[int, int]:
    now = time.time()
    return int(now // WINDOW_SECONDS), max(1, WINDOW_SECONDS - int(now % WINDOW_SECONDS))


def _count_locally(key: str, window: int, add: int) -> int:
    global _local_window
    if window != _local_window:
        _local_counts.clear()
        _local_window = window
    _local_counts[key] = _local_counts.get(key, 0) + add
    return _local_counts[key]


async def _count(redis: Any, key: str, window: int, add: int, fail_closed: bool) -> int | None:
    if redis is not None:
        try:
            if add:
                async with redis.pipeline(transaction=True) as pipe:
                    pipe.incrby(key, add)
                    pipe.expire(key, WINDOW_SECONDS * 2)
                    count, _ = await pipe.execute()
            else:
                count = await redis.get(key)
            return int(count or 0)
        except Exception as err:
            metrics.RATE_LIMIT_REDIS_ERRORS.inc()
            logger.warning("rate_limit.redis_unavailable", error=type(err).__name__)
    return _count_locally(key, window, add) if fail_closed else None


def refusal(limit: str, retry_after: int) -> dict[str, Any]:
    metrics.RATE_LIMITED.labels(limit).inc()
    return {
        "status_code": 429,
        "detail": f"Too many requests; retry in {retry_after} seconds",
        "headers": {"Retry-After": str(retry_after)},
    }


def refusal_response(limit: str, retry_after: int) -> JSONResponse:
    answer = refusal(limit, retry_after)
    return JSONResponse({"detail": answer["detail"]}, status_code=429, headers=answer["headers"])


async def over(
    request: Request, limit: str, ident: str, allowed: int, *, add: int = 1, fail_closed: bool
) -> int | None:
    """Count this request; seconds to wait when the window is already full, else None."""
    if not get_settings().rate_limit_enabled:
        return None
    window, retry_after = _window()
    key = f"{PREFIX}{limit}:{ident}:{window}"
    count = await _count(_redis(request), key, window, add, fail_closed)
    if count is None:
        return None
    return retry_after if count > allowed or (not add and count >= allowed) else None


async def failing_source(request: Request) -> int | None:
    """Seconds to wait if this address has failed authentication too often this minute."""
    settings = get_settings()
    return await over(
        request,
        AUTH_FAILURES,
        client_address(request),
        settings.rate_limit_auth_failures_per_minute,
        add=0,
        fail_closed=True,
    )


async def count_failure(request: Request) -> None:
    settings = get_settings()
    await over(
        request,
        AUTH_FAILURES,
        client_address(request),
        settings.rate_limit_auth_failures_per_minute,
        fail_closed=True,
    )


async def sign_in_attempt(request: Request) -> int | None:
    return await over(
        request,
        SIGN_IN,
        client_address(request),
        get_settings().rate_limit_sign_in_per_minute,
        fail_closed=True,
    )


async def principal_wait(request: Request, principal: Principal) -> int | None:
    """Seconds to wait when the key or session has used this minute's requests. Counted once."""
    if getattr(request.state, "rate_limited_principal", False):
        return None
    request.state.rate_limited_principal = True
    ident = f"session:{principal.session_id}" if principal.session_id else f"key:{principal.key_id}"
    return await over(
        request,
        PRINCIPAL,
        ident,
        get_settings().rate_limit_requests_per_minute,
        fail_closed=False,
    )


async def principal_request(request: Request, principal: Principal) -> None:
    wait = await principal_wait(request, principal)
    if wait is not None:
        raise HTTPException(**refusal(PRINCIPAL, wait))


async def webhook(request: Request, connection: str) -> None:
    """429 when a verified connection has sent its webhooks for this minute."""
    wait = await over(
        request,
        WEBHOOK,
        connection,
        get_settings().rate_limit_webhooks_per_minute,
        fail_closed=False,
    )
    if wait is not None:
        raise HTTPException(**refusal(WEBHOOK, wait))
