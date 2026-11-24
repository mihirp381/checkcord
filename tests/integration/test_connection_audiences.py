"""Audiences an admin sets for connections whose source says nothing about who may read."""

from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app_role_helpers import app_database_url, create_app_role, drop_app_role
from fastapi.testclient import TestClient
from key_helpers import KeySpec, seeded_keys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.core.config import get_settings
from checkcord.main import app
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.services import connection_audiences, doctor
from checkcord.storage import audit
from checkcord.storage.postgres import PostgresEventStore, create_checkcord_engine, tenant_context

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ADMIN_URL = get_settings().database_url
A = "/api/v1/admin/connections/audience"
SCIM = "/scim/v2"
TABLES = (
    "connection_audiences",
    "jira_installations",
    "audit_log_entries",
    "scim_groups",
    "context_event_links",
    "canonical_contexts",
    "events",
)


@dataclass
class World:
    tenant_id: UUID
    engine: AsyncEngine
    key: str
    tokens: dict[str, str]
    emails: dict[str, str]
    people: dict[str, UUID]

    def auth(self, who: str = "admin") -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[who]}"}


async def _admin(sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    engine = create_async_engine(ADMIN_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            result = await conn.execute(text(sql), params or {})
            return list(result.fetchall()) if result.returns_rows else []
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(name="w")
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[World, None]:
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://checkcord.example.test")
    get_settings.cache_clear()
    tenant_id = uuid4()
    tag = tenant_id.hex[:8]
    host = f"aud-{tag}.atlassian.net"
    tokens = {who: f"conn_aud_{who}_{uuid4().hex}" for who in ("admin", "alice", "bob")}
    emails = {who: f"{who}-{tag}@acme.example" for who in ("alice", "bob")}
    specs = [
        KeySpec(tokens["admin"], tenant_id, admin=True),
        KeySpec(tokens["alice"], tenant_id, user_email=emails["alice"]),
        KeySpec(tokens["bob"], tenant_id, user_email=emails["bob"]),
    ]
    await _admin(
        "INSERT INTO jira_installations (jira_host, tenant_id, personal_access_token, "
        "webhook_secret) VALUES (:h, :t, :p, :s)",
        {"h": host, "t": tenant_id, "p": f"x_{uuid4().hex}", "s": uuid4().hex},
    )
    async with seeded_keys(specs) as users:
        role, url = await create_app_role()
        engine = create_checkcord_engine(url)
        try:
            with app_database_url(url):
                yield World(
                    tenant_id,
                    engine,
                    f"jira:{host}",
                    tokens,
                    emails,
                    {who: users[email] for who, email in emails.items()},
                )
        finally:
            await engine.dispose()
            await drop_app_role(role)
            await _admin("DELETE FROM scim_group_members WHERE tenant_id = :t", {"t": tenant_id})
            for table in TABLES:
                await _admin(f"DELETE FROM {table} WHERE tenant_id = :t", {"t": tenant_id})
            get_settings.cache_clear()


@contextmanager
def _client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


async def _record(w: World, container: str = "pay") -> UUID:
    event = CanonicalEvent(
        tenant_id=w.tenant_id,
        source_platform=SourcePlatform.JIRA,
        source_type=SourceType.TICKET_CREATED,
        source_id=f"{container}-{uuid4().hex[:6]}",
        happened_at=datetime.now(UTC),
        author=AuthorInfo(source_id="someone"),
        content_raw="Refunds settle within two days.",
        content_clean="Refunds settle within two days.",
        access_control=AccessControl(is_public=True),
        audience_container=container,
        connection=w.key,
    )
    async with tenant_context(w.engine, w.tenant_id) as conn:
        store = PostgresEventStore(conn)
        assert await store.save_event(event)
        record = await store.save_canonical_context(
            w.tenant_id, "Refunds settle within two days.", 1.0, base_event_id=event.event_id
        )
        await store.link_context_to_event(w.tenant_id, record, event.event_id)
    return record


def _readers(w: World, record: UUID) -> set[str]:
    readers = set()
    with _client() as c:
        for who in ("alice", "bob"):
            res = c.get(
                f"/api/v1/graph/node/context:{record}?repo_identifier=acme/x", headers=w.auth(who)
            )
            assert res.status_code in (200, 404), res.text
            if res.status_code == 200:
                readers.add(who)
    return readers


def _set(w: World, who: str = "admin", **body: Any) -> Any:
    with _client() as c:
        return c.put(A, json={"connection": w.key, **body}, headers=w.auth(who))


def _scim(w: World, method: str, path: str, body: dict[str, Any]) -> Any:
    headers = {**w.auth(), "Content-Type": "application/scim+json; charset=utf-8"}
    with _client() as c:
        return c.request(method, f"{SCIM}{path}", json=body, headers=headers)


async def test_unset_connection_is_readable_by_nobody_and_the_doctor_says_so(w: World) -> None:
    record = await _record(w)
    assert _readers(w, record) == set()
    [check] = await doctor.connection_audience_checks(w.engine, w.tenant_id)
    assert check.warn and check.detail == (
        f"1 record(s) from {w.key} are readable by nobody; set an audience with "
        "`checkcord connections audience`"
    )


@pytest.mark.parametrize(
    ("body", "readers"),
    [
        ({"mode": "everyone"}, {"alice", "bob"}),
        ({"mode": "people", "people": []}, {"alice"}),
        ({"mode": "nobody"}, set()),
    ],
    ids=["everyone", "people", "nobody"],
)
async def test_each_mode_applies_at_ingest(
    w: World, body: dict[str, Any], readers: set[str]
) -> None:
    if "people" in body:
        body = {**body, "people": [w.emails["alice"].upper()]}
    assert _set(w, **body).status_code == 200
    assert _readers(w, await _record(w)) == readers


async def test_group_mode_applies_at_ingest_and_follows_the_group(w: World) -> None:
    created = _scim(
        w,
        "POST",
        "/Groups",
        {"displayName": "Payments-Team", "members": [{"value": str(w.people["alice"])}]},
    )
    assert created.status_code == 201, created.text
    assert _set(w, mode="groups", groups=["payments-team"]).status_code == 200
    record = await _record(w)
    assert _readers(w, record) == {"alice"}

    add = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [
            {"op": "add", "path": "members", "value": [{"value": str(w.people["bob"])}]}
        ],
    }
    assert _scim(w, "PATCH", f"/Groups/{created.json()['id']}", add).status_code == 204
    assert _readers(w, record) == {"alice", "bob"}


async def test_a_change_recomputes_what_was_ingested_and_is_audited(w: World) -> None:
    records = [await _record(w) for _ in range(3)]
    assert all(_readers(w, r) == set() for r in records)

    res = _set(w, mode="everyone")
    assert res.status_code == 200 and res.json()["mode"] == "everyone"
    assert all(_readers(w, r) == {"alice", "bob"} for r in records)
    with _client() as c:
        [shown] = c.get(f"{A}?connection={w.key}", headers=w.auth()).json()["items"]
    assert shown["recompute"]["state"] == "done" and shown["recompute"]["done"] == 3

    _set(w, mode="people", people=[w.emails["bob"]])
    assert all(_readers(w, r) == {"bob"} for r in records)
    rows = await _admin(
        "SELECT params FROM audit_log_entries WHERE tenant_id = :t "
        "AND action = 'connection.audience_changed' ORDER BY created_at",
        {"t": w.tenant_id},
    )
    assert [(r[0]["before"]["mode"], r[0]["after"]["mode"]) for r in rows] == [
        ("nobody", "everyone"),
        ("everyone", "people"),
    ]
    assert rows[1][0]["after"]["people"] == [w.emails["bob"]]
    assert rows[1][0]["after"]["members"] == 1


async def test_a_container_setting_wins_over_the_connection(w: World) -> None:
    _set(w, mode="everyone")
    _set(w, mode="people", container="PAY", people=[w.emails["alice"]])
    pay, ops = await _record(w, "pay"), await _record(w, "ops")
    assert _readers(w, pay) == {"alice"}
    assert _readers(w, ops) == {"alice", "bob"}

    _set(w, mode="nobody")
    assert _readers(w, pay) == {"alice"}
    assert _readers(w, ops) == set()


async def test_only_an_admin_may_set_an_audience(w: World) -> None:
    assert _set(w, "alice", mode="everyone").status_code == 403
    with _client() as c:
        assert c.get(f"{A}?connection={w.key}", headers=w.auth("alice")).status_code == 403
    assert (
        await _admin("SELECT 1 FROM connection_audiences WHERE tenant_id = :t", {"t": w.tenant_id})
        == []
    )


async def test_a_source_audience_connection_is_refused(w: World) -> None:
    with pytest.raises(ValueError, match="takes its audience from its source"):
        await connection_audiences.set_audience(
            w.engine, w.tenant_id, "slack:T1", "everyone", audit.CLI_ACTOR
        )
