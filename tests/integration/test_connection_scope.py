"""Per-connection scope: out-of-scope material is dropped before it is stored or published."""

import hashlib
import hmac
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app_role_helpers import app_database_url, create_app_role, drop_app_role
from fastapi.testclient import TestClient
from key_helpers import KeySpec, seeded_keys
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.adapters.chat.providers.slack.listeners import register_listeners
from checkcord.adapters.code.providers.github import poller as github_poller
from checkcord.adapters.tasks.providers.linear import router as linear
from checkcord.adapters.tasks.providers.notion import router as notion
from checkcord.core.config import get_settings
from checkcord.main import app
from checkcord.services import scope
from checkcord.storage import audit
from checkcord.storage.postgres import create_checkcord_engine

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ADMIN_URL = get_settings().database_url
A = "/api/v1/admin"
TABLES = (
    "connection_scopes",
    "connection_scope_skips",
    "jira_installations",
    "slack_installations",
    "msteams_installations",
    "notion_installations",
    "linear_installations",
    "github_installations",
    "audit_log_entries",
    "events",
)


@dataclass
class World:
    tenant_id: UUID
    other_tenant: UUID
    engine: AsyncEngine
    redis: Redis
    tokens: dict[str, str]
    names: dict[str, str]
    jira_secret: str

    def auth(self, who: str = "admin") -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[who]}"}


class CaptureBus:
    def __init__(self) -> None:
        self.published: list[Any] = []
        self.redis = None

    async def publish(self, topic: str, event: Any) -> str:
        self.published.append(event)
        return "0-0"


async def _admin(sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    engine = create_async_engine(ADMIN_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            result = await conn.execute(text(sql), params or {})
            return list(result.fetchall()) if result.returns_rows else []
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def world(async_redis_client: Redis) -> AsyncGenerator[World, None]:
    tenant_id, other = uuid4(), uuid4()
    tag = tenant_id.hex[:8]
    names = {
        "jira": f"scope-{tag}.atlassian.net",
        "slack": f"T{tag.upper()}",
        "teams": f"ms-{tag}",
        "notion": f"ws-{tag}",
        "linear": f"org-{tag}",
        "github": f"acme-{tag}/billing",
    }
    jira_secret = f"jira_{uuid4().hex}"
    tokens = {who: f"scope_{who}_{uuid4().hex}" for who in ("user", "admin", "other_admin")}
    specs = [
        KeySpec(tokens["user"], tenant_id, user_email=f"u-{tag}@acme.example"),
        KeySpec(tokens["admin"], tenant_id, admin=True),
        KeySpec(tokens["other_admin"], other, admin=True),
    ]
    p = {"t": tenant_id, **names, "secret": jira_secret, "token": f"x_{uuid4().hex}"}
    await _admin(
        "INSERT INTO jira_installations (jira_host, tenant_id, personal_access_token, "
        "webhook_secret) VALUES (:jira, :t, :token, :secret)",
        p,
    )
    await _admin(
        "INSERT INTO slack_installations (team_id, tenant_id, bot_token) "
        "VALUES (:slack, :t, :token)",
        p,
    )
    await _admin(
        "INSERT INTO msteams_installations (ms_tenant_id, tenant_id, auth_payload) "
        "VALUES (:teams, :t, '{}')",
        p,
    )
    await _admin(
        "INSERT INTO notion_installations (notion_workspace_id, tenant_id, access_token) "
        "VALUES (:notion, :t, :token)",
        p,
    )
    await _admin(
        "INSERT INTO linear_installations (linear_organization_id, tenant_id, access_token) "
        "VALUES (:linear, :t, :token)",
        p,
    )
    await _admin(
        "INSERT INTO github_installations (repo_identifier, tenant_id, personal_access_token, "
        "webhook_secret) VALUES (:github, :t, :token, :secret)",
        p,
    )
    async with seeded_keys(specs):
        role, url = await create_app_role()
        engine = create_checkcord_engine(url)
        try:
            with app_database_url(url):
                yield World(
                    tenant_id, other, engine, async_redis_client, tokens, names, jira_secret
                )
        finally:
            await engine.dispose()
            await drop_app_role(role)
            for table in TABLES:
                await _admin(
                    f"DELETE FROM {table} WHERE tenant_id IN (:t, :o)", {"t": tenant_id, "o": other}
                )
            scope._cache.clear()


def _issue(w: World, key: str) -> bytes:
    return json.dumps(
        {
            "timestamp": 1719225840000,
            "webhookEvent": "jira:issue_created",
            "issue": {
                "id": "10002",
                "key": key,
                "self": f"https://{w.names['jira']}/rest/api/2/issue/10002",
                "fields": {
                    "summary": "Retries stay idempotent",
                    "project": {"key": key.partition("-")[0]},
                    "status": {"name": "To Do"},
                    "issuetype": {"id": "1", "name": "Task", "subtask": False},
                },
            },
        }
    ).encode()


def _jira(client: TestClient, w: World, key: str) -> Any:
    body = _issue(w, key)
    sig = hmac.new(w.jira_secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/api/v1/webhooks/jira",
        content=body,
        headers={"X-Hub-Signature-256": f"sha256={sig}", "Content-Type": "application/json"},
    )


async def test_jira_webhook_skips_excluded_projects_and_counts_them(world: World) -> None:
    w = world
    key = f"jira:{w.names['jira']}"
    await w.redis.delete("events.raw.tasks")
    with TestClient(app) as client:
        listed = client.get(f"{A}/connections", headers=w.auth()).json()["items"]
        [jira] = [c for c in listed if c["connection"] == key]
        assert jira["scope"] == {"include": [], "exclude": []} and jira["scope_skipped"] == 0

        res = client.put(
            f"{A}/connections/scope",
            json={"connection": key, "exclude": ["PAY"]},
            headers=w.auth(),
        )
        assert res.status_code == 200, res.text
        assert res.json()["exclude"] == ["pay"] and res.json()["only_listed"] is False

        skipped = _jira(client, w, "PAY-7")
        assert skipped.status_code == 200 and skipped.json() == {"status": "out_of_scope"}
        assert await w.redis.xlen("events.raw.tasks") == 0
        assert _jira(client, w, "ENG-42").status_code == 202
        assert await w.redis.xlen("events.raw.tasks") == 1

        shown = client.get(f"{A}/connections/scope", params={"connection": key}, headers=w.auth())
        assert shown.json()["skipped"] == 1
        stats = client.get(f"{A}/stats", headers=w.auth()).json()
        assert stats["scope_skipped"] == 1
        [row] = [c for c in stats["connections"] if c["connection"] == key]
        assert row["scope_skipped"] == 1

        res = client.put(
            f"{A}/connections/scope",
            json={"connection": key, "include": ["eng"], "exclude": []},
            headers=w.auth(),
        )
        assert res.json()["only_listed"] is True
        assert _jira(client, w, "OPS-1").json() == {"status": "out_of_scope"}
        assert _jira(client, w, "ENG-43").status_code == 202

    [(params, channel)] = await _admin(
        "SELECT params, channel FROM audit_log_entries WHERE tenant_id = :t "
        "AND action = 'connection.scope_changed' ORDER BY created_at LIMIT 1",
        {"t": w.tenant_id},
    )
    assert channel == audit.API
    assert params == {
        "connection": key,
        "include": [],
        "exclude": ["pay"],
        "before": {"include": [], "exclude": []},
    }
    await w.redis.delete("events.raw.tasks")


async def test_scope_is_admin_only_per_tenant_and_per_supported_connector(world: World) -> None:
    w = world
    key = f"jira:{w.names['jira']}"
    with TestClient(app) as client:
        body = {"connection": key, "exclude": ["PAY"]}
        assert (
            client.put(f"{A}/connections/scope", json=body, headers=w.auth("user")).status_code
            == 403
        )
        assert (
            client.put(
                f"{A}/connections/scope", json=body, headers=w.auth("other_admin")
            ).status_code
            == 404
        )
        unsupported = client.put(
            f"{A}/connections/scope",
            json={"connection": "custom:push", "exclude": ["x"]},
            headers=w.auth(),
        )
        assert unsupported.status_code == 422 and "no scope" in unsupported.text
        assert client.put(f"{A}/connections/scope", json=body, headers=w.auth()).status_code == 200
        other = client.get(
            f"{A}/connections/scope", params={"connection": key}, headers=w.auth("other_admin")
        )
        assert other.json()["exclude"] == []

    other_engine = create_async_engine(ADMIN_URL)
    try:
        assert await scope.allows(other_engine, w.other_tenant, key, "PAY")
    finally:
        await other_engine.dispose()


async def test_scope_change_keeps_what_was_already_ingested(world: World) -> None:
    w = world
    key = f"jira:{w.names['jira']}"
    await w.redis.delete("events.raw.tasks")
    with TestClient(app) as client:
        assert _jira(client, w, "PAY-1").status_code == 202
        listed = client.get(f"{A}/connections", headers=w.auth()).json()["items"]
        before = next(c["events"] for c in listed if c["connection"] == key)
        client.put(
            f"{A}/connections/scope",
            json={"connection": key, "exclude": ["PAY"]},
            headers=w.auth(),
        )
        listed = client.get(f"{A}/connections", headers=w.auth()).json()["items"]
        assert next(c["events"] for c in listed if c["connection"] == key) == before
    assert await w.redis.xlen("events.raw.tasks") == 1
    await w.redis.delete("events.raw.tasks")


async def test_pollers_skip_out_of_scope_containers_before_fetching(world: World) -> None:
    w = world
    actor = audit.Actor(channel=audit.API)
    db_in, db_out = "1a2b3c4d" * 4, "9f8e7d6c" * 4
    await scope.change(
        w.engine, w.tenant_id, f"notion:{w.names['notion']}", actor, exclude=[db_out.upper()]
    )
    await scope.change(w.engine, w.tenant_id, f"linear:{w.names['linear']}", actor, include=["ENG"])
    await scope.change(
        w.engine, w.tenant_id, f"github:{w.names['github']}", actor, exclude=[w.names["github"]]
    )

    hyphenated = f"{db_out[:8]}-{db_out[8:12]}-{db_out[12:16]}-{db_out[16:20]}-{db_out[20:]}"
    edited = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    pages = [
        {"id": "page-in", "parent": {"database_id": db_in}, "properties": {}},
        {"id": "page-out", "parent": {"database_id": hyphenated}, "properties": {}},
    ]
    pages = [{**page, "last_edited_time": edited} for page in pages]
    fetched: list[str] = []

    async def blocks(self: Any, page_id: str) -> list[dict[str, Any]]:
        fetched.append(page_id)
        return []

    bus = CaptureBus()
    with (
        patch.object(notion, "_search_page", AsyncMock(return_value=(pages, None))),
        patch.object(notion.NotionAdapter, "fetch_page_blocks", blocks),
        patch.object(notion, "app_bypass_rls_context", _only(w)),
    ):
        await notion.poll_notion_workspaces(w.engine, bus)
    assert fetched == ["page-in"]

    issues = [
        {"identifier": "ENG-1", "title": "a", "team": {"id": "t-eng", "key": "ENG"}},
        {"identifier": "OPS-1", "title": "b", "team": {"id": "t-ops", "key": "OPS"}},
    ]
    issues = [{**issue, "id": issue["identifier"], "updatedAt": edited} for issue in issues]
    linear_bus = CaptureBus()
    with (
        patch.object(linear, "_issues_page", AsyncMock(return_value=(issues, None))),
        patch.object(linear, "app_bypass_rls_context", _only(w)),
    ):
        assert await linear.poll_linear_organizations(w.engine, linear_bus) == 1
    assert ["[ENG-1]" in e.content_clean for e in linear_bus.published] == [True]

    sent = AsyncMock(return_value=MagicMock(status_code=500, text=""))
    with patch.object(github_poller.http_retry, "send", sent):
        await github_poller.poll_github_history(w.engine, CaptureBus())
    assert not [c for c in sent.await_args_list if w.names["github"] in c.args[3]]

    skips = await _admin(
        "SELECT count(*) FROM connection_scope_skips WHERE tenant_id = :t", {"t": w.tenant_id}
    )
    assert skips == [(0,)]


async def test_chat_listeners_drop_excluded_channels(world: World) -> None:
    w = world
    actor = audit.Actor(channel=audit.API)
    await scope.change(w.engine, w.tenant_id, f"slack:{w.names['slack']}", actor, exclude=["C_OUT"])
    await scope.change(
        w.engine, w.tenant_id, f"teams:{w.names['teams']}", actor, include=["19:in@thread.tacv2"]
    )

    handlers: dict[str, Any] = {}

    class App:
        def event(self, name: str) -> Any:
            return lambda fn: handlers.setdefault(name, fn)

        def action(self, name: str) -> Any:
            return lambda fn: fn

        def view(self, name: str) -> Any:
            return lambda fn: fn

    bus = CaptureBus()
    register_listeners(App(), bus, w.engine)
    client = AsyncMock()
    await handlers["message"](
        {
            "type": "message",
            "team": w.names["slack"],
            "channel": "c_out",
            "user": "U1",
            "text": "x",
        },
        client,
    )
    assert bus.published == [] and client.mock_calls == []

    activity = {
        "type": "message",
        "id": "1",
        "tenantId": w.names["teams"],
        "conversation": {"id": "19:out@thread.tacv2;messageid=1", "conversationType": "channel"},
        "channelData": {"team": {"id": "19:team@thread.tacv2"}},
        "from": {"id": "29:x"},
        "text": "hello",
    }
    await w.redis.delete("events.raw.chat")
    with (
        patch(
            "checkcord.adapters.chat.providers.msteams.listeners.verify_bot_token",
            AsyncMock(return_value=None),
        ),
        TestClient(app) as http,
    ):
        assert http.post("/api/messages", json=activity).status_code == 200
    assert await w.redis.xlen("events.raw.chat") == 0

    [(n,)] = await _admin(
        "SELECT coalesce(sum(skipped), 0) FROM connection_scope_skips WHERE tenant_id = :t",
        {"t": w.tenant_id},
    )
    assert n == 2


def _only(w: World) -> Any:
    from contextlib import asynccontextmanager

    from checkcord.storage.postgres import app_bypass_rls_context

    @asynccontextmanager
    async def scoped(engine: Any) -> Any:
        async with app_bypass_rls_context(engine) as conn:
            yield _Filtered(conn, w.tenant_id)

    return scoped


class _Filtered:
    """Hands a poller only this test's installation rows."""

    def __init__(self, conn: Any, tenant_id: UUID) -> None:
        self.conn = conn
        self.tenant_id = tenant_id

    async def execute(self, stmt: Any, *args: Any) -> Any:
        result = await self.conn.execute(stmt, *args)
        rows = [r for r in result.fetchall() if r.tenant_id == self.tenant_id]
        return MagicMock(fetchall=MagicMock(return_value=rows))
