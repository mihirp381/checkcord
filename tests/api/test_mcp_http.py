import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from app_role_helpers import app_database_url, create_app_role, drop_app_role
from fastapi.testclient import TestClient
from key_helpers import KeySpec, delete_contexts, seed_context, seeded_keys

from checkcord.main import app
from checkcord.proxy import mcp_server

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

PUBLIC, ALICE_ONLY, BOB_ONLY, OTHER_TENANT = (
    "PUBLIC-DECISION",
    "ALICE-ONLY-DECISION",
    "BOB-ONLY-DECISION",
    "TENANT-B-DECISION",
)


@dataclass(frozen=True)
class World:
    tenant_a: UUID
    tenant_b: UUID
    alice_key: str = "mcp_key_alice"
    unbound_key: str = "mcp_key_unbound"
    admin_key: str = "mcp_key_admin_unbound"
    tenant_b_key: str = "mcp_key_tenant_b"


@pytest_asyncio.fixture
async def world() -> AsyncGenerator[World, None]:
    w = World(tenant_a=uuid4(), tenant_b=uuid4())
    specs = [
        KeySpec(w.alice_key, w.tenant_a, user_email="alice@acme.example"),
        KeySpec("mcp_key_bob", w.tenant_a, user_email="bob@acme.example"),
        KeySpec(w.unbound_key, w.tenant_a),
        KeySpec(w.admin_key, w.tenant_a, admin=True),
        KeySpec(w.tenant_b_key, w.tenant_b, user_email="alice@other.example"),
    ]
    async with seeded_keys(specs) as users:
        await seed_context(w.tenant_a, PUBLIC, public=True)
        await seed_context(
            w.tenant_a, ALICE_ONLY, public=False, allowed=[users["alice@acme.example"]]
        )
        await seed_context(w.tenant_a, BOB_ONLY, public=False, allowed=[users["bob@acme.example"]])
        await seed_context(w.tenant_b, OTHER_TENANT, public=True)
        role, url = await create_app_role()
        try:
            with app_database_url(url):
                yield w
        finally:
            await drop_app_role(role)
            await delete_contexts(w.tenant_a, w.tenant_b)


_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def _call(client: TestClient, token: str | None, tool: str, args: dict[str, Any]) -> Any:
    headers = dict(_MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        },
    )


def _summaries(client: TestClient, token: str) -> set[str]:
    res = _call(client, token, "view_active_decisions", {})
    assert res.status_code == 200, res.text
    body = res.json()["result"]
    assert not body.get("isError"), body
    payload = json.loads(body["content"][0]["text"])
    return {d["summary"] for d in payload["decisions"]}


async def test_mcp_requires_api_key(world: World) -> None:
    with TestClient(app) as client:
        assert _call(client, None, "view_active_decisions", {}).status_code == 401
        assert _call(client, "not-a-key", "view_active_decisions", {}).status_code == 401
        res = client.post(
            "/mcp",
            headers={**_MCP_HEADERS, "Authorization": "Basic abc"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert res.status_code == 401
        assert res.headers["www-authenticate"] == "Bearer"


async def test_mcp_lists_the_three_tools(world: World) -> None:
    with TestClient(app) as client:
        res = client.post(
            "/mcp",
            headers={**_MCP_HEADERS, "Authorization": f"Bearer {world.alice_key}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    assert res.status_code == 200, res.text
    names = {t["name"] for t in res.json()["result"]["tools"]}
    assert names == {"get_dependency_flow", "view_active_decisions", "search_conceptual_context"}


async def test_user_bound_key_sees_only_what_the_user_may_see(world: World) -> None:
    with TestClient(app) as client:
        seen = _summaries(client, world.alice_key)
    assert seen == {PUBLIC, ALICE_ONLY}


async def test_unbound_key_sees_only_public(world: World) -> None:
    with TestClient(app) as client:
        assert _summaries(client, world.unbound_key) == {PUBLIC}


async def test_admin_key_is_not_a_bypass(world: World) -> None:
    with TestClient(app) as client:
        assert _summaries(client, world.admin_key) == {PUBLIC}


async def test_keys_never_cross_tenants(world: World) -> None:
    with TestClient(app) as client:
        assert OTHER_TENANT not in _summaries(client, world.alice_key)
        assert _summaries(client, world.tenant_b_key) == {OTHER_TENANT}


async def test_stdio_forwards_to_http_endpoint(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHECKCORD_PROXY_API_URL", "http://checkcord.test")
    monkeypatch.setenv("CHECKCORD_PROXY_API_KEY", world.alice_key)

    def _client(api_key: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://checkcord.test",
            headers={"Authorization": f"Bearer {api_key}"},
        )

    monkeypatch.setattr(mcp_server, "_remote_http_client", _client)

    async with app.router.lifespan_context(app):
        result = json.loads(await mcp_server.view_active_decisions())
        empty = json.loads(await mcp_server.search_conceptual_context("   "))

    assert {d["summary"] for d in result["decisions"]} == {PUBLIC, ALICE_ONLY}
    assert "error" in empty


async def test_stdio_reports_a_rejected_key(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECKCORD_PROXY_API_URL", "http://checkcord.test")
    monkeypatch.setenv("CHECKCORD_PROXY_API_KEY", "revoked-key")

    def _client(api_key: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://checkcord.test",
            headers={"Authorization": f"Bearer {api_key}"},
        )

    monkeypatch.setattr(mcp_server, "_remote_http_client", _client)
    async with app.router.lifespan_context(app):
        result = json.loads(await mcp_server.view_active_decisions())
    assert "error" in result
