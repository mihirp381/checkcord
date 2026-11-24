import argparse
import asyncio
import csv
import io
import json
import logging
import secrets
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app_role_helpers import app_database_url, create_app_role, drop_app_role
from fastapi.testclient import TestClient
from key_helpers import KeySpec, delete_contexts, seeded_keys
from sqlalchemy import delete, insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

import checkcord.cli.commands as cli
from checkcord.adapters.tasks.providers.jira.connector import connect_jira
from checkcord.core.config import get_settings
from checkcord.core.crypto import hash_api_key
from checkcord.main import app
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import AccessVisibility, AclType, SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.services import keys, mcp_tools
from checkcord.services.connections import disconnect
from checkcord.storage import audit
from checkcord.storage.models import (
    AgentLedger,
    APIKey,
    ASTContextEdge,
    ASTNode,
    AuditLogEntry,
    ContextEventLink,
    EnterpriseUser,
    JiraInstallation,
    Skill,
)
from checkcord.storage.postgres import (
    PostgresEventStore,
    app_bypass_rls_context,
    create_checkcord_engine,
    events_table,
    tenant_context,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

REPO = "acme/audit"
FILE = "src/total.py"
SYMBOL = "computeTotal"
SECRET_SUMMARY = "AUDIT-SUMMARY-must-not-reach-the-log"
ALICE = "alice@audit.test"
VECTOR = [1.0] + [0.0] * 1023


def _token() -> str:
    return "chk_" + secrets.token_urlsafe(24)


@dataclass(frozen=True)
class World:
    tenant_a: UUID
    tenant_b: UUID
    alice: str
    admin: str
    admin_b: str
    alice_id: UUID
    ctx: UUID
    event: UUID
    ast_node: UUID
    ledger: UUID
    skill: UUID


class _Embedder:
    async def embed_text(self, text: str) -> list[float]:
        return VECTOR


@pytest.fixture(autouse=True)
def _fake_embedder() -> Iterator[None]:
    with patch("checkcord.api.mcp_http._embedder", return_value=_Embedder()):
        yield


async def _seed(tenant: UUID) -> tuple[UUID, UUID, UUID, UUID, UUID]:
    engine = create_async_engine(get_settings().database_url)
    ast_node, ledger, skill, event = uuid4(), uuid4(), uuid4(), uuid4()
    try:
        async with tenant_context(engine, tenant) as conn:
            store = PostgresEventStore(conn)
            ctx = await store.save_canonical_context(
                tenant_id=tenant,
                summary=SECRET_SUMMARY,
                confidence_score=9.0,
                context_type="adr",
                embedding=VECTOR,
                access_control={"is_public": True, "visibility": "public", "acl_type": "custom"},
            )
            await store.save_event(
                CanonicalEvent(
                    event_id=event,
                    tenant_id=tenant,
                    source_platform=SourcePlatform.SLACK,
                    source_type=SourceType.MESSAGE,
                    source_id=f"msg-{event}",
                    happened_at=datetime.now(UTC),
                    author=AuthorInfo(source_id="U1", display_name="Someone"),
                    content_raw="raw",
                    content_clean="clean",
                    access_control=AccessControl(
                        visibility=AccessVisibility.PUBLIC, acl_type=AclType.CUSTOM, is_public=True
                    ),
                )
            )
            await store.link_context_to_event(tenant, ctx, event)
            await conn.execute(
                insert(ASTNode).values(
                    id=ast_node,
                    tenant_id=tenant,
                    repo_identifier=REPO,
                    file_path=FILE,
                    symbol_name=SYMBOL,
                    node_type="function",
                    merkle_hash="h",
                )
            )
            await conn.execute(
                insert(ASTContextEdge).values(
                    ast_node_id=ast_node, canonical_id=ctx, tenant_id=tenant
                )
            )
            await conn.execute(
                insert(AgentLedger).values(
                    id=ledger,
                    tenant_id=tenant,
                    scope_type="project",
                    scope_id="p1",
                    key="k",
                    value={},
                )
            )
            await conn.execute(
                insert(Skill).values(
                    id=skill,
                    tenant_id=tenant,
                    name="audit skill",
                    description="d",
                    instructions="i",
                )
            )
    finally:
        await engine.dispose()
    return ctx, event, ast_node, ledger, skill


async def _cleanup(*tenants: UUID) -> None:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with app_bypass_rls_context(engine) as conn:
            for model in (
                AuditLogEntry,
                ASTContextEdge,
                ASTNode,
                AgentLedger,
                Skill,
                ContextEventLink,
                JiraInstallation,
                APIKey,
                EnterpriseUser,
            ):
                await conn.execute(delete(model).where(model.tenant_id.in_(tenants)))
            await conn.execute(delete(events_table).where(events_table.c.tenant_id.in_(tenants)))
    finally:
        await engine.dispose()
    await delete_contexts(*tenants)


@pytest_asyncio.fixture
async def world() -> AsyncGenerator[World, None]:
    tenant_a, tenant_b = uuid4(), uuid4()
    alice, admin, admin_b = _token(), _token(), _token()
    specs = [
        KeySpec(alice, tenant_a, user_email=ALICE, name="alice key"),
        KeySpec(admin, tenant_a, admin=True),
        KeySpec(admin_b, tenant_b, admin=True),
    ]
    try:
        async with seeded_keys(specs) as users:
            ctx, event, ast_node, ledger, skill = await _seed(tenant_a)
            role, url = await create_app_role()
            try:
                with app_database_url(url):
                    yield World(
                        tenant_a=tenant_a,
                        tenant_b=tenant_b,
                        alice=alice,
                        admin=admin,
                        admin_b=admin_b,
                        alice_id=users[ALICE],
                        ctx=ctx,
                        event=event,
                        ast_node=ast_node,
                        ledger=ledger,
                        skill=skill,
                    )
            finally:
                await drop_app_role(role)
    finally:
        await _cleanup(tenant_a, tenant_b)


async def _entries(tenant: UUID, action: str | None = None) -> list[dict[str, Any]]:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with tenant_context(engine, tenant) as conn:
            stmt = select(AuditLogEntry.__table__).where(AuditLogEntry.tenant_id == tenant)
            if action:
                stmt = stmt.where(AuditLogEntry.action == action)
            rows = await conn.execute(stmt.order_by(AuditLogEntry.created_at))
            return [dict(r) for r in rows.mappings()]
    finally:
        await engine.dispose()


async def _key_id(token: str) -> UUID:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with app_bypass_rls_context(engine) as conn:
            key_id: UUID = await conn.scalar(
                select(APIKey.id).where(APIKey.hashed_token == hash_api_key(token))
            )
            return key_id
    finally:
        await engine.dispose()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mcp(client: TestClient, token: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    res = client.post(
        "/mcp",
        headers={**_auth(token), "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        },
    )
    assert res.status_code == 200, res.text
    result: dict[str, Any] = res.json()["result"]
    return result


async def _only(tenant: UUID, action: str) -> dict[str, Any]:
    rows = await _entries(tenant, action)
    assert len(rows) == 1, (action, rows)
    return rows[0]


async def test_each_read_path_writes_one_entry_with_person_key_and_records(world: World) -> None:
    alice_key = await _key_id(world.alice)
    ctx = str(world.ctx)
    with TestClient(app) as client:
        _mcp(client, world.alice, "get_dependency_flow", {"file_path": FILE})
        _mcp(client, world.alice, "view_active_decisions", {"repo_identifier": REPO})
        found = _mcp(client, world.alice, "search_conceptual_context", {"query": SECRET_SUMMARY})
        assert SECRET_SUMMARY in found["content"][0]["text"]
        for path in (
            f"/api/v1/graph/context?symbol={SYMBOL}",
            f"/api/v1/graph/impact?symbol={SYMBOL}",
            f"/api/v1/graph/decisions/active?repo_identifier={REPO}",
            f"/api/v1/graph/network?repo_identifier={REPO}",
            f"/api/v1/graph/node/context:{ctx}?repo_identifier={REPO}",
            "/api/v1/agents/ledger?scope_type=project",
            "/api/v1/agents/skills?query=audit",
        ):
            assert client.get(path, headers=_auth(world.alice)).status_code == 200, path

    expected = {
        "mcp.get_dependency_flow": ("mcp", [ctx]),
        "mcp.view_active_decisions": ("mcp", [ctx]),
        "mcp.search_conceptual_context": ("mcp", [ctx]),
        "graph.context": ("api", [ctx]),
        "graph.impact": ("api", [ctx]),
        "graph.active_decisions": ("api", [ctx]),
        "graph.network": ("api", [ctx]),
        "graph.node": ("api", [ctx, str(world.event)]),
        "ledger.read": ("api", [str(world.ledger)]),
        "skills.read": ("api", [str(world.skill)]),
    }
    rows = await _entries(world.tenant_a)
    assert sorted(r["action"] for r in rows) == sorted(expected)
    for row in rows:
        channel, record_ids = expected[row["action"]]
        assert row["channel"] == channel, row
        assert row["user_id"] == world.alice_id and row["user_email"] == ALICE
        assert row["key_id"] == alice_key and row["key_name"] == "alice key"
        assert sorted(row["record_ids"]) == sorted(record_ids), row
        assert row["result_count"] == 1, row
    search = next(r for r in rows if r["action"] == "mcp.search_conceptual_context")
    assert search["params"]["query"] == SECRET_SUMMARY


async def test_a_failed_audit_write_fails_the_read(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    broken = audit.Actor(channel="not-a-channel")
    with (
        patch("checkcord.api.deps.Principal.as_actor", return_value=broken),
        TestClient(app, raise_server_exceptions=False) as client,
        caplog.at_level(logging.INFO, logger="checkcord.audit"),
    ):
        res = client.get(
            f"/api/v1/graph/node/context:{world.ctx}?repo_identifier={REPO}",
            headers=_auth(world.alice),
        )
        assert res.status_code == 500
        assert SECRET_SUMMARY not in res.text
        result = _mcp(client, world.alice, "view_active_decisions", {"repo_identifier": REPO})
        assert SECRET_SUMMARY not in json.dumps(result)
        assert "error" in result["content"][0]["text"]

    engine = create_checkcord_engine(get_settings().database_url)
    try:
        out = await mcp_tools.active_decisions(engine, world.tenant_a, broken, REPO)
    finally:
        await engine.dispose()
    assert SECRET_SUMMARY not in out and "error" in json.loads(out)
    assert await _entries(world.tenant_a) == []
    assert not [r for r in caplog.records if r.name == "checkcord.audit"]


async def test_checkcord_app_can_only_insert_and_select(world: World) -> None:
    engine = create_checkcord_engine(get_settings().database_url)
    try:
        async with tenant_context(engine, world.tenant_a) as conn:
            await audit.record(conn, world.tenant_a, audit.CLI_ACTOR, "test.entry")
        async with tenant_context(engine, world.tenant_a) as conn:
            assert await conn.scalar(text("SELECT count(*) FROM audit_log_entries")) == 1
        for sql in (
            "UPDATE audit_log_entries SET action = 'changed'",
            "DELETE FROM audit_log_entries",
            "TRUNCATE audit_log_entries",
        ):
            with pytest.raises(DBAPIError, match="permission denied"):
                async with tenant_context(engine, world.tenant_a) as conn:
                    await conn.execute(text(sql))
    finally:
        await engine.dispose()
    assert [r["action"] for r in await _entries(world.tenant_a)] == ["test.entry"]


async def test_one_tenant_cannot_read_or_write_anothers_audit(world: World) -> None:
    engine = create_checkcord_engine(get_settings().database_url)
    try:
        async with tenant_context(engine, world.tenant_a) as conn:
            await audit.record(conn, world.tenant_a, audit.CLI_ACTOR, "test.entry")
        async with tenant_context(engine, world.tenant_b) as conn:
            assert await conn.scalar(text("SELECT count(*) FROM audit_log_entries")) == 0
        with pytest.raises(DBAPIError, match="row-level security"):
            async with tenant_context(engine, world.tenant_b) as conn:
                await audit.record(conn, world.tenant_a, audit.CLI_ACTOR, "test.forged")
    finally:
        await engine.dispose()

    with TestClient(app) as client:
        res = client.get("/api/v1/admin/audit", headers=_auth(world.admin_b))
        assert res.status_code == 200
        actions = [e["action"] for e in res.json()["items"]]
        assert "test.entry" not in actions
        res = client.get("/api/v1/admin/audit/export", headers=_auth(world.admin_b))
        assert "test.entry" not in res.text
        assert client.get("/api/v1/admin/audit", headers=_auth(world.alice)).status_code == 403


async def test_list_filters_pagination_and_exports(world: World) -> None:
    alice_key = await _key_id(world.alice)
    before = datetime.now(UTC) - timedelta(seconds=1)
    with TestClient(app) as client:
        for _ in range(3):
            client.get(f"/api/v1/graph/context?symbol={SYMBOL}", headers=_auth(world.alice))
        client.get("/api/v1/agents/skills", headers=_auth(world.alice))

        def page(**params: Any) -> dict[str, Any]:
            res = client.get("/api/v1/admin/audit", params=params, headers=_auth(world.admin))
            assert res.status_code == 200, res.text
            body: dict[str, Any] = res.json()
            return body

        seen: list[str] = []
        cursor = None
        while True:
            body = page(user=ALICE, limit=2, **({"cursor": cursor} if cursor else {}))
            seen += [e["id"] for e in body["items"]]
            cursor = body["next_cursor"]
            if not cursor:
                break
        assert len(seen) == len(set(seen)) == 4

        newest = page(user=ALICE)["items"]
        assert [e["action"] for e in newest] == ["skills.read"] + ["graph.context"] * 3
        assert len(page(key=str(alice_key))["items"]) == 4
        assert len(page(key="alice key")["items"]) == 4
        assert len(page(action="graph.context", channel="api")["items"]) == 3
        assert len(page(record_id=str(world.ctx))["items"]) == 3
        assert page(channel="mcp")["items"] == []
        assert page(until=before.isoformat())["items"] == []
        assert len(page(since=before.isoformat(), user=ALICE.upper())["items"]) == 4
        assert (
            client.get("/api/v1/admin/audit?cursor=nope", headers=_auth(world.admin)).status_code
            == 400
        )

        jsonl = client.get(
            "/api/v1/admin/audit/export",
            params={"since": before.isoformat()},
            headers=_auth(world.admin),
        )
        assert jsonl.headers["content-type"].startswith("application/x-ndjson")
        lines = [json.loads(line) for line in jsonl.text.splitlines()]
        exported_csv = client.get(
            "/api/v1/admin/audit/export", params={"format": "csv"}, headers=_auth(world.admin)
        )
        assert exported_csv.headers["content-type"].startswith("text/csv")
        rows = list(csv.DictReader(io.StringIO(exported_csv.text)))
        filtered = client.get(
            "/api/v1/admin/audit/export",
            params={"user": ALICE, "action": "graph.context"},
            headers=_auth(world.admin),
        )
        only = [json.loads(line) for line in filtered.text.splitlines()]

    reads = [entry for entry in lines if entry["user_email"] == ALICE]
    assert [entry["action"] for entry in reads] == ["graph.context"] * 3 + ["skills.read"]
    assert reads[0]["record_ids"] == [str(world.ctx)]
    assert {r["action"] for r in rows} >= {"graph.context", "skills.read", "audit.read"}
    assert next(r for r in rows if r["action"] == "graph.context")["record_ids"] == str(world.ctx)

    reads_of_audit = await _entries(world.tenant_a, "audit.read")
    assert len(reads_of_audit) >= 10
    assert all(r["key_name"] == "test key" for r in reads_of_audit)
    exports = await _entries(world.tenant_a, "audit.export")
    assert [e["params"].get("format") for e in exports] == ["jsonl", "csv", "jsonl"]
    assert [e["action"] for e in only] == ["graph.context"] * 3
    assert exports[-1]["params"]["user"] == ALICE


async def test_cli_audit_list_and_export(
    world: World, capsys: pytest.CaptureFixture[str], tmp_path: Any
) -> None:
    with TestClient(app) as client:
        client.get(f"/api/v1/graph/context?symbol={SYMBOL}", headers=_auth(world.alice))
    tenant = str(world.tenant_a)

    await asyncio.to_thread(cli.main, ["audit", "list", "--tenant", tenant, "--user", ALICE])
    listed = capsys.readouterr().out
    assert "graph.context" in listed and ALICE in listed and SECRET_SUMMARY not in listed

    await asyncio.to_thread(cli.main, ["audit", "export", "--tenant", tenant, "--format", "jsonl"])
    exported = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert "graph.context" in [e["action"] for e in exported]

    target = tmp_path / "audit.csv"
    await asyncio.to_thread(
        cli.main,
        ["audit", "export", "--tenant", tenant, "--format", "csv", "--output", str(target)],
    )
    assert "Wrote" in capsys.readouterr().out
    assert "graph.context" in target.read_text()

    rows = await _entries(world.tenant_a)
    cli_rows = [r for r in rows if r["channel"] == "cli"]
    assert [r["action"] for r in cli_rows] == ["audit.read", "audit.export", "audit.export"]
    assert all(r["key_id"] is None and r["user_id"] is None for r in cli_rows)


async def test_admin_actions_are_recorded(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    tenant = world.tenant_a
    admin_key = await _key_id(world.admin)
    engine = create_checkcord_engine(get_settings().database_url)
    try:
        key_id, _ = await keys.create_key(
            engine, tenant, "ci", admin=False, user_email="new@audit.test", create_user=True
        )
        assert await keys.revoke_key(engine, tenant, key_id)
        init_id, _ = await keys.init_admin_key(engine, tenant, force=True)
        host = f"acme-{uuid4().hex[:8]}.atlassian.net"
        await connect_jira(engine, tenant, host, "jira-pat", False, [])
        assert await disconnect(engine, tenant, "jira", host)
    finally:
        await engine.dispose()
    await asyncio.to_thread(
        cli.cmd_events_retry, argparse.Namespace(tenant=tenant, all=True, ids=None)
    )
    capsys.readouterr()

    revoked = _token()
    async with seeded_keys([KeySpec(revoked, tenant)]):
        await _revoke(revoked)
        with TestClient(app) as client:
            assert client.get("/api/v1/agents/skills", headers=_auth(revoked)).status_code == 401
            assert client.get("/api/v1/agents/skills", headers=_auth(revoked)).status_code == 401
            scim = client.post(
                "/scim/v2/Users",
                headers=_auth(world.admin),
                json={
                    "userName": "bob@audit.test",
                    "emails": [{"value": "bob@audit.test", "primary": True}],
                    "active": True,
                },
            )
            assert scim.status_code == 201, scim.text
            bob = scim.json()["id"]
            patched = client.patch(
                f"/scim/v2/Users/{bob}",
                headers=_auth(world.admin),
                json={
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                    "Operations": [{"op": "replace", "path": "active", "value": False}],
                },
            )
            assert patched.status_code == 200, patched.text
            override = client.post(
                f"/api/v1/admin/events/{world.event}/acl-override",
                headers=_auth(world.admin),
                json={"admin_overrides": [str(world.alice_id)]},
            )
            assert override.status_code == 200, override.text
            scope = client.patch(
                f"/api/v1/admin/records/{world.ctx}/scope",
                headers=_auth(world.admin),
                json={"repo_identifiers": [REPO], "is_locked": False},
            )
            assert scope.status_code == 200, scope.text
            history = client.get(
                f"/api/v1/graph/context/{world.ctx}/scope/history", headers=_auth(world.admin)
            )
            assert history.json()["changed_by"]
            raw = client.get(
                f"/api/v1/admin/raw-content/context:{world.ctx}?repo_identifier={REPO}",
                headers=_auth(world.admin),
            )
            assert SECRET_SUMMARY in raw.text

    by_action = {r["action"]: r for r in await _entries(tenant)}
    assert by_action["key.created"]["record_ids"] == [str(key_id)]
    assert by_action["key.created"]["params"]["admin"] is False
    assert by_action["key.created"]["params"]["user_email"] == "new@audit.test"
    assert by_action["key.revoked"]["params"]["user_email"] == "new@audit.test"
    assert by_action["user.created"]["channel"] in ("cli", "scim")
    assert by_action["key.revoked"]["record_ids"] == [str(key_id)]
    assert by_action["key.init"]["record_ids"] == [str(init_id)]
    assert by_action["connection.added"]["params"] == {"provider": "jira", "identifier": host}
    assert by_action["connection.removed"]["params"] == {"provider": "jira", "identifier": host}
    assert by_action["events.retry"]["result_count"] == 0
    for action in ("key.created", "key.revoked", "key.init", "connection.added", "events.retry"):
        assert by_action[action]["channel"] == "cli" and by_action[action]["key_id"] is None

    failures = await _entries(tenant, "auth.failed")
    assert len(failures) == 1
    assert failures[0]["params"]["reason"] == "revoked key"
    assert failures[0]["params"]["key_prefix"] == revoked[:8]
    assert revoked not in json.dumps(failures[0]["params"])

    scim_rows = [r for r in await _entries(tenant) if r["channel"] == "scim"]
    assert [r["action"] for r in scim_rows] == ["user.created", "user.deactivated"]
    assert all(r["record_ids"] == [bob] and r["key_id"] == admin_key for r in scim_rows)
    assert by_action["event.audience_override"]["record_ids"] == [str(world.event)]
    assert by_action["context.scope_changed"]["record_ids"] == [str(world.ctx)]
    assert by_action["context.scope_changed"]["params"]["repo_identifiers"] == [REPO]
    assert by_action["admin.raw_content"]["key_id"] == admin_key


async def _revoke(token: str) -> None:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with app_bypass_rls_context(engine) as conn:
            await conn.execute(
                APIKey.__table__.update()
                .where(APIKey.hashed_token == hash_api_key(token))
                .values(active=False)
            )
    finally:
        await engine.dispose()


async def test_log_lines_carry_no_record_text(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    with TestClient(app) as client, caplog.at_level(logging.INFO, logger="checkcord.audit"):
        _mcp(client, world.alice, "view_active_decisions", {"repo_identifier": REPO})
        client.get(
            f"/api/v1/graph/node/context:{world.ctx}?repo_identifier={REPO}",
            headers=_auth(world.alice),
        )
        client.get(
            f"/api/v1/admin/raw-content/context:{world.ctx}?repo_identifier={REPO}",
            headers=_auth(world.admin),
        )
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == "checkcord.audit"]
    assert [line["action"] for line in lines] == [
        "mcp.view_active_decisions",
        "graph.node",
        "admin.raw_content",
    ]
    assert all(line["logger"] == "checkcord.audit" for line in lines)
    assert all(str(world.ctx) in line["record_ids"] for line in lines)
    assert SECRET_SUMMARY not in json.dumps(lines)
