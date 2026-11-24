from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, quote, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.adapters.tasks.providers.linear.router import poll_linear_organizations
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core.config import get_settings
from checkcord.core.crypto import decrypt_secret, encrypt_token
from checkcord.core.oauth_state import mint_state, read_state
from checkcord.main import app
from checkcord.models.event import CanonicalEvent

pytestmark = pytest.mark.integration


def _state(tenant_id: UUID, ttl: int = 900) -> str:
    return quote(mint_state("linear", tenant_id, ttl=ttl), safe="")


def _bad_state(kind: str) -> str:
    if kind == "missing":
        return ""
    token = {
        "tenant-uuid": str(uuid4()),
        "tampered": mint_state("linear", uuid4())[:-4] + "AAAA",
        "expired": mint_state("linear", uuid4(), ttl=-1),
        "other": mint_state("slack", uuid4()),
    }[kind]
    return quote(token, safe="")


ORGANIZATION_ID = "test-linear-org"


async def _drain_redis_stream(redis_url: str, topic: str) -> list[dict[str, Any]]:
    client = Redis.from_url(redis_url)
    try:
        raw_streams: Any = await client.xread(streams={topic: "0-0"}, count=100)
        events: list[dict[str, Any]] = []
        if raw_streams:
            for _, messages in raw_streams:
                for _msg_id, fields in messages:
                    decoded = {
                        (k.decode("utf-8") if isinstance(k, bytes) else k): (
                            v.decode("utf-8") if isinstance(v, bytes) else v
                        )
                        for k, v in fields.items()
                    }
                    event = CanonicalEvent.from_redis_dict(decoded)
                    events.append(event.model_dump(mode="json"))
        return events
    finally:
        await client.aclose()


async def _flush_redis_stream(redis_url: str, topic: str) -> None:
    client = Redis.from_url(redis_url)
    try:
        await client.delete(topic)
    finally:
        await client.aclose()


def test_linear_install_requires_configured_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINEAR_CLIENT_ID", raising=False)
    monkeypatch.delenv("LINEAR_REDIRECT_URI", raising=False)
    with TestClient(app) as client:
        resp = client.get(
            f"/api/v1/webhooks/linear/install?state={_state(uuid4())}", follow_redirects=False
        )
    assert resp.status_code == 500


def test_linear_install_redirects_with_state(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = uuid4()
    monkeypatch.setenv("LINEAR_CLIENT_ID", "client-abc")
    monkeypatch.setenv("LINEAR_REDIRECT_URI", "http://test/redirect")

    with TestClient(app) as client:
        resp = client.get(
            f"/api/v1/webhooks/linear/install?state={_state(tenant_id)}", follow_redirects=False
        )
    assert resp.status_code in (302, 307)
    state = parse_qs(urlsplit(resp.headers["location"]).query)["state"][0]
    assert read_state(state, "linear").tenant_id == tenant_id


@pytest.mark.parametrize("bad", ["missing", "tenant-uuid", "tampered", "expired", "other"])
def test_linear_install_rejects_bad_state(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("LINEAR_CLIENT_ID", "client-abc")
    monkeypatch.setenv("LINEAR_REDIRECT_URI", "http://test/redirect")
    with TestClient(app) as client:
        resp = client.get(
            f"/api/v1/webhooks/linear/install?state={_bad_state(bad)}", follow_redirects=False
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_linear_oauth_redirect_saves_installation(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = uuid4()
    monkeypatch.setenv("LINEAR_CLIENT_ID", "client-abc")
    monkeypatch.setenv("LINEAR_CLIENT_SECRET", "secret-abc")
    monkeypatch.setenv("LINEAR_REDIRECT_URI", "http://test/redirect")

    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    token_response = httpx.Response(
        200,
        json={"access_token": "linear-token-xyz"},
        request=httpx.Request("POST", "https://api.linear.app/oauth/token"),
    )
    org_response = httpx.Response(
        200,
        json={"data": {"viewer": {"organization": {"id": ORGANIZATION_ID}}}},
        request=httpx.Request("POST", "https://api.linear.app/graphql"),
    )

    state = _state(tenant_id)
    try:
        with patch("httpx.AsyncClient.post", AsyncMock(side_effect=[token_response, org_response])):
            with TestClient(app) as client:
                resp = client.get(
                    f"/api/v1/webhooks/linear/oauth_redirect?code=abc123&state={state}"
                )

        assert resp.status_code == 200, resp.text
        assert "Linear is connected to Checkcord. You can close this tab." in resp.text

        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            row = (
                await conn.execute(
                    text(
                        "SELECT tenant_id, access_token FROM linear_installations "
                        "WHERE linear_organization_id = :oid"
                    ),
                    {"oid": ORGANIZATION_ID},
                )
            ).fetchone()
            assert row is not None
            assert row.tenant_id == tenant_id
            assert row.access_token != "linear-token-xyz"  # stored encrypted, not raw
            assert decrypt_secret(row.access_token) == "linear-token-xyz"

        with TestClient(app) as client:
            replay = client.get(f"/api/v1/webhooks/linear/oauth_redirect?code=abc123&state={state}")
        assert replay.status_code == 400
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM linear_installations WHERE linear_organization_id = :oid"),
                {"oid": ORGANIZATION_ID},
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_linear_oauth_redirect_invalid_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINEAR_CLIENT_ID", "client-abc")
    monkeypatch.setenv("LINEAR_CLIENT_SECRET", "secret-abc")

    with TestClient(app) as client:
        for bad in ("not-a-uuid", str(uuid4()), _bad_state("tampered"), _bad_state("expired")):
            resp = client.get(f"/api/v1/webhooks/linear/oauth_redirect?code=abc123&state={bad}")
            assert resp.status_code == 400, bad
        missing = client.get("/api/v1/webhooks/linear/oauth_redirect?code=abc123")
        assert missing.status_code == 400


@pytest.mark.asyncio
async def test_poll_linear_organizations_publishes_document_events() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    tenant_id = uuid4()

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text(
                "INSERT INTO linear_installations "
                "(id, linear_organization_id, tenant_id, access_token) "
                "VALUES (:id, :oid, :tid, :token)"
            ),
            {
                "id": uuid4(),
                "oid": ORGANIZATION_ID,
                "tid": tenant_id,
                "token": encrypt_token("linear-token-xyz"),
            },
        )

    redis_client = Redis.from_url(settings.redis_url)
    bus = RedisStreamsEventBus(redis_client)

    fake_issue = {
        "identifier": "ENG-1",
        "title": "Fix the thing",
        "description": "Some description text",
        "state": {"name": "Todo"},
        "priority": 2,
        "updatedAt": "2099-01-01T00:00:00.000Z",
        "comments": {"nodes": []},
    }

    try:
        with patch(
            "checkcord.adapters.tasks.providers.linear.router._issues_page",
            AsyncMock(return_value=([fake_issue], None)),
        ):
            published = await poll_linear_organizations(engine, bus)

        assert published == 1
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            saved = await conn.scalar(
                text(
                    "SELECT poll_cursor FROM linear_installations "
                    "WHERE linear_organization_id = :oid"
                ),
                {"oid": ORGANIZATION_ID},
            )
        assert saved.isoformat() == "2099-01-01T00:00:00+00:00"

        events = await _drain_redis_stream(settings.redis_url, "events.raw.tasks")
        assert len(events) == 1
        ev = events[0]
        assert ev["tenant_id"] == str(tenant_id)
        assert ev["source_platform"] == "linear"
        assert "ENG-1" in ev["document_metadata"]["title"]
        assert "Some description text" in ev["content_clean"]
    finally:
        await _flush_redis_stream(settings.redis_url, "events.raw.tasks")
        await redis_client.aclose()
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM linear_installations WHERE linear_organization_id = :oid"),
                {"oid": ORGANIZATION_ID},
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_poll_linear_organizations_swallows_per_organization_failures() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    tenant_id = uuid4()

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text(
                "INSERT INTO linear_installations "
                "(id, linear_organization_id, tenant_id, access_token) "
                "VALUES (:id, :oid, :tid, :token)"
            ),
            {
                "id": uuid4(),
                "oid": ORGANIZATION_ID,
                "tid": tenant_id,
                "token": encrypt_token("linear-token-xyz"),
            },
        )

    try:
        with patch(
            "checkcord.adapters.tasks.providers.linear.router._issues_page",
            AsyncMock(side_effect=Exception("boom")),
        ):
            published = await poll_linear_organizations(engine, AsyncMock())
        assert published == 0
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM linear_installations WHERE linear_organization_id = :oid"),
                {"oid": ORGANIZATION_ID},
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_linear_install_cannot_move_to_another_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, intruder = uuid4(), uuid4()
    monkeypatch.setenv("LINEAR_CLIENT_ID", "client-abc")
    monkeypatch.setenv("LINEAR_CLIENT_SECRET", "secret-abc")
    monkeypatch.setenv("LINEAR_REDIRECT_URI", "http://test/redirect")
    engine = create_async_engine(get_settings().database_url)

    def responses() -> list[httpx.Response]:
        return [
            httpx.Response(
                200,
                json={"access_token": "linear-token-xyz"},
                request=httpx.Request("POST", "https://api.linear.app/oauth/token"),
            ),
            httpx.Response(
                200,
                json={"data": {"viewer": {"organization": {"id": ORGANIZATION_ID}}}},
                request=httpx.Request("POST", "https://api.linear.app/graphql"),
            ),
        ]

    url = "/api/v1/webhooks/linear/oauth_redirect?code=abc123&state="
    try:
        with TestClient(app) as client:
            with patch("httpx.AsyncClient.post", AsyncMock(side_effect=responses())):
                assert client.get(url + _state(owner)).status_code == 200
            with patch("httpx.AsyncClient.post", AsyncMock(side_effect=responses())):
                again = client.get(url + _state(owner))
            with patch("httpx.AsyncClient.post", AsyncMock(side_effect=responses())):
                taken = client.get(url + _state(intruder))
        assert again.status_code == 200
        assert taken.status_code == 409

        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            tenants = (
                (
                    await conn.execute(
                        text(
                            "SELECT tenant_id FROM linear_installations "
                            "WHERE linear_organization_id = :id"
                        ),
                        {"id": ORGANIZATION_ID},
                    )
                )
                .scalars()
                .all()
            )
        assert tenants == [owner]
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM linear_installations WHERE linear_organization_id = :id"),
                {"id": ORGANIZATION_ID},
            )
        await engine.dispose()
