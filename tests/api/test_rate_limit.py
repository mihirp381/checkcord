from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import fakeredis.aioredis
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from checkcord.api import rate_limit
from checkcord.api.deps import Principal
from checkcord.core.config import get_settings
from checkcord.main import create_app

WINDOW_START = 1_800_000_000.0


class BrokenRedis:
    def pipeline(self, **_: Any) -> Any:
        raise RedisConnectionError("down")

    async def get(self, _key: str) -> Any:
        raise RedisConnectionError("down")


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, float]]:
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "3")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURES_PER_MINUTE", "2")
    monkeypatch.setenv("RATE_LIMIT_SIGN_IN_PER_MINUTE", "2")
    monkeypatch.setenv("RATE_LIMIT_WEBHOOKS_PER_MINUTE", "4")
    get_settings.cache_clear()
    clock = {"now": WINDOW_START + 10}
    with patch.object(rate_limit.time, "time", lambda: clock["now"]):
        yield clock


def _app(redis: Any) -> TestClient:
    app = create_app()
    app.state.ctx = SimpleNamespace(redis_client=redis, db_engine=None)
    return TestClient(app)


def _request(redis: Any, host: str = "203.0.113.7") -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(ctx=SimpleNamespace(redis_client=redis))),
        client=SimpleNamespace(host=host),
        state=SimpleNamespace(),
    )


def test_failed_auth_from_one_address_is_refused_then_recovers(limits: dict[str, float]) -> None:
    client = _app(fakeredis.aioredis.FakeRedis())
    assert [client.post("/mcp").status_code for _ in range(2)] == [401, 401]
    refused = client.post("/mcp")
    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == "50"
    assert client.get("/healthz").status_code == 200

    limits["now"] += 60
    assert client.post("/mcp").status_code == 401


def test_failed_auth_limit_holds_without_redis(limits: dict[str, float]) -> None:
    client = _app(BrokenRedis())
    assert [client.post("/mcp").status_code for _ in range(3)] == [401, 401, 429]


def test_sign_in_attempts_are_limited_per_address(limits: dict[str, float]) -> None:
    client = _app(fakeredis.aioredis.FakeRedis())
    codes = [client.get("/api/v1/auth/login").status_code for _ in range(3)]
    assert 429 not in codes[:2] and codes[2] == 429
    limits["now"] += 60
    assert client.get("/api/v1/auth/login").status_code != 429


def test_limits_off_lets_everything_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURES_PER_MINUTE", "1")
    get_settings.cache_clear()
    client = _app(fakeredis.aioredis.FakeRedis())
    assert {client.post("/mcp").status_code for _ in range(5)} == {401}


@pytest.mark.asyncio
async def test_principal_limit_triggers_and_recovers(limits: dict[str, float]) -> None:
    redis = fakeredis.aioredis.FakeRedis()
    principal = Principal(tenant_id=uuid4(), key_name="k", key_id=uuid4())
    for _ in range(3):
        await rate_limit.principal_request(_request(redis), principal)
    with pytest.raises(HTTPException) as refused:
        await rate_limit.principal_request(_request(redis), principal)
    assert refused.value.status_code == 429
    assert refused.value.headers == {"Retry-After": "50"}

    other = Principal(tenant_id=principal.tenant_id, key_name="k", key_id=uuid4())
    await rate_limit.principal_request(_request(redis), other)

    limits["now"] += 60
    await rate_limit.principal_request(_request(redis), principal)


@pytest.mark.asyncio
async def test_sessions_are_counted_apart_from_keys(limits: dict[str, float]) -> None:
    redis = fakeredis.aioredis.FakeRedis()
    session = Principal(tenant_id=uuid4(), key_name="s", key_id=uuid4(), session_id=uuid4())
    for _ in range(3):
        await rate_limit.principal_request(_request(redis), session)
    assert await rate_limit.principal_wait(_request(redis), session) is not None
    assert await redis.get(f"ratelimit:principal:session:{session.session_id}:30000000") == b"4"


@pytest.mark.asyncio
async def test_one_request_is_counted_once(limits: dict[str, float]) -> None:
    redis = fakeredis.aioredis.FakeRedis()
    principal = Principal(tenant_id=uuid4(), key_name="k", key_id=uuid4())
    request = _request(redis)
    for _ in range(10):
        await rate_limit.principal_request(request, principal)


@pytest.mark.asyncio
async def test_principal_limit_fails_open_without_redis(limits: dict[str, float]) -> None:
    principal = Principal(tenant_id=uuid4(), key_name="k", key_id=uuid4())
    for _ in range(10):
        await rate_limit.principal_request(_request(BrokenRedis()), principal)


@pytest.mark.asyncio
async def test_webhook_limit_is_per_connection(limits: dict[str, float]) -> None:
    redis = fakeredis.aioredis.FakeRedis()
    for _ in range(4):
        await rate_limit.webhook(_request(redis), "github:acme/api")
    with pytest.raises(HTTPException) as refused:
        await rate_limit.webhook(_request(redis), "github:acme/api")
    assert refused.value.status_code == 429
    await rate_limit.webhook(_request(redis), "github:acme/web")
    for _ in range(10):
        await rate_limit.webhook(_request(BrokenRedis()), "github:acme/api")
    limits["now"] += 60
    await rate_limit.webhook(_request(redis), "github:acme/api")
