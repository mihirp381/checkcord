from datetime import UTC, datetime, timedelta
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

from checkcord.adapters.tasks.providers.notion.router import poll_notion_workspaces
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core.config import get_settings
from checkcord.core.crypto import decrypt_secret, encrypt_token
from checkcord.core.oauth_state import mint_state, read_state
from checkcord.main import app
from checkcord.models.event import CanonicalEvent

pytestmark = pytest.mark.integration


def _state(tenant_id: UUID, ttl: int = 900) -> str:
    return quote(mint_state("notion", tenant_id, ttl=ttl), safe="")


def _bad_state(kind: str) -> str:
    if kind == "missing":
        return ""
    token = {
        "tenant-uuid": str(uuid4()),
        "tampered": mint_state("notion", uuid4())[:-4] + "AAAA",
        "expired": mint_state("notion", uuid4(), ttl=-1),
        "other": mint_state("slack", uuid4()),
    }[kind]
    return quote(token, safe="")


WORKSPACE_ID = "test-notion-workspace"


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


def test_notion_install_requires_configured_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTION_CLIENT_ID", raising=False)
    monkeypatch.delenv("NOTION_REDIRECT_URI", raising=False)
    with TestClient(app) as client:
        resp = client.get(
            f"/api/v1/webhooks/notion/install?state={_state(uuid4())}", follow_redirects=False
        )
    assert resp.status_code == 500


def test_notion_install_redirects_with_state(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = uuid4()
    monkeypatch.setenv("NOTION_CLIENT_ID", "client-abc")
    monkeypatch.setenv("NOTION_REDIRECT_URI", "http://test/redirect")

    with TestClient(app) as client:
        resp = client.get(
            f"/api/v1/webhooks/notion/install?state={_state(tenant_id)}", follow_redirects=False
        )
    assert resp.status_code in (302, 307)
    state = parse_qs(urlsplit(resp.headers["location"]).query)["state"][0]
    assert read_state(state, "notion").tenant_id == tenant_id


@pytest.mark.parametrize("bad", ["missing", "tenant-uuid", "tampered", "expired", "other"])
def test_notion_install_rejects_bad_state(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("NOTION_CLIENT_ID", "client-abc")
    monkeypatch.setenv("NOTION_REDIRECT_URI", "http://test/redirect")
    with TestClient(app) as client:
        resp = client.get(
            f"/api/v1/webhooks/notion/install?state={_bad_state(bad)}", follow_redirects=False
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_notion_oauth_redirect_saves_installation(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = uuid4()
    monkeypatch.setenv("NOTION_CLIENT_ID", "client-abc")
    monkeypatch.setenv("NOTION_CLIENT_SECRET", "secret-abc")
    monkeypatch.setenv("NOTION_REDIRECT_URI", "http://test/redirect")

    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    mock_response = httpx.Response(
        200,
        json={"access_token": "notion-token-xyz", "workspace_id": WORKSPACE_ID},
        request=httpx.Request("POST", "https://api.notion.com/v1/oauth/token"),
    )

    state = _state(tenant_id)
    try:
        with patch("httpx.AsyncClient.post", AsyncMock(return_value=mock_response)):
            with TestClient(app) as client:
                resp = client.get(
                    f"/api/v1/webhooks/notion/oauth_redirect?code=abc123&state={state}"
                )

        assert resp.status_code == 200, resp.text
        assert "Notion is connected to Checkcord. You can close this tab." in resp.text

        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            row = (
                await conn.execute(
                    text(
                        "SELECT tenant_id, access_token FROM notion_installations "
                        "WHERE notion_workspace_id = :wid"
                    ),
                    {"wid": WORKSPACE_ID},
                )
            ).fetchone()
            assert row is not None
            assert row.tenant_id == tenant_id
            assert row.access_token != "notion-token-xyz"  # stored encrypted, not raw
            assert decrypt_secret(row.access_token) == "notion-token-xyz"

        with TestClient(app) as client:
            replay = client.get(f"/api/v1/webhooks/notion/oauth_redirect?code=abc123&state={state}")
        assert replay.status_code == 400
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM notion_installations WHERE notion_workspace_id = :wid"),
                {"wid": WORKSPACE_ID},
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_notion_oauth_redirect_invalid_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_CLIENT_ID", "client-abc")
    monkeypatch.setenv("NOTION_CLIENT_SECRET", "secret-abc")

    with TestClient(app) as client:
        for bad in ("not-a-uuid", str(uuid4()), _bad_state("tampered"), _bad_state("expired")):
            resp = client.get(f"/api/v1/webhooks/notion/oauth_redirect?code=abc123&state={bad}")
            assert resp.status_code == 400, bad
        missing = client.get("/api/v1/webhooks/notion/oauth_redirect?code=abc123")
        assert missing.status_code == 400


@pytest.mark.asyncio
async def test_poll_notion_workspaces_publishes_document_events() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    tenant_id = uuid4()

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text(
                "INSERT INTO notion_installations "
                "(id, notion_workspace_id, tenant_id, access_token) "
                "VALUES (:id, :wid, :tid, :token)"
            ),
            {
                "id": uuid4(),
                "wid": WORKSPACE_ID,
                "tid": tenant_id,
                "token": encrypt_token("notion-token-xyz"),
            },
        )

    redis_client = Redis.from_url(settings.redis_url)
    bus = RedisStreamsEventBus(redis_client)

    fake_page = {
        "id": "page-1",
        "url": "https://notion.so/page-1",
        "last_edited_time": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        "properties": {"title": {"type": "title", "title": [{"plain_text": "My Page"}]}},
    }
    fake_blocks = [
        {
            "id": "b1",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"plain_text": "Hello from Notion"}]},
            "has_children": False,
        }
    ]

    try:
        with (
            patch(
                "checkcord.adapters.tasks.providers.notion.router._search_page",
                AsyncMock(return_value=([fake_page], None)),
            ),
            patch(
                "checkcord.adapters.notion_adapter.NotionAdapter.fetch_page_blocks",
                AsyncMock(return_value=fake_blocks),
            ),
        ):
            published = await poll_notion_workspaces(engine, bus)

        assert published == 1

        events = await _drain_redis_stream(settings.redis_url, "events.raw.tasks")
        assert len(events) == 1
        ev = events[0]
        assert ev["tenant_id"] == str(tenant_id)
        assert ev["source_platform"] == "notion"
        assert "Hello from Notion" in ev["content_clean"]
        assert ev["document_metadata"]["url"] == "https://notion.so/page-1"
    finally:
        await _flush_redis_stream(settings.redis_url, "events.raw.tasks")
        await redis_client.aclose()
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM notion_installations WHERE notion_workspace_id = :wid"),
                {"wid": WORKSPACE_ID},
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_poll_notion_workspaces_swallows_per_installation_failures() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    tenant_id = uuid4()

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text(
                "INSERT INTO notion_installations "
                "(id, notion_workspace_id, tenant_id, access_token) "
                "VALUES (:id, :wid, :tid, :token)"
            ),
            {
                "id": uuid4(),
                "wid": WORKSPACE_ID,
                "tid": tenant_id,
                "token": encrypt_token("notion-token-xyz"),
            },
        )

    try:
        with patch(
            "checkcord.adapters.tasks.providers.notion.router._search_page",
            AsyncMock(side_effect=Exception("boom")),
        ):
            # Must not raise -- a single installation failure shouldn't abort the sweep.
            published = await poll_notion_workspaces(engine, AsyncMock())
        assert published == 0
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM notion_installations WHERE notion_workspace_id = :wid"),
                {"wid": WORKSPACE_ID},
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_notion_install_cannot_move_to_another_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, intruder = uuid4(), uuid4()
    monkeypatch.setenv("NOTION_CLIENT_ID", "client-abc")
    monkeypatch.setenv("NOTION_CLIENT_SECRET", "secret-abc")
    monkeypatch.setenv("NOTION_REDIRECT_URI", "http://test/redirect")
    engine = create_async_engine(get_settings().database_url)

    def responses() -> list[httpx.Response]:
        return [
            httpx.Response(
                200,
                json={"access_token": "notion-token-xyz", "workspace_id": WORKSPACE_ID},
                request=httpx.Request("POST", "https://api.notion.com/v1/oauth/token"),
            )
        ]

    url = "/api/v1/webhooks/notion/oauth_redirect?code=abc123&state="
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
                            "SELECT tenant_id FROM notion_installations "
                            "WHERE notion_workspace_id = :id"
                        ),
                        {"id": WORKSPACE_ID},
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
                text("DELETE FROM notion_installations WHERE notion_workspace_id = :id"),
                {"id": WORKSPACE_ID},
            )
        await engine.dispose()
