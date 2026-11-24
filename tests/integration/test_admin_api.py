"""The admin HTTP API: admin only, one tenant at a time, audited, no secrets in answers."""

import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from key_helpers import seed_context
from redis.asyncio import Redis
from sign_in_helpers import World, admin_sql, api, audit_rows, bearer, signed_in, world

from checkcord.bus import dead_letter
from checkcord.core.crypto import hash_api_key
from checkcord.main import app
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.services import sessions
from checkcord.storage.postgres import PostgresEventStore, tenant_context

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

__all__ = ["world"]

DLQ = "events.admin-test.dlq"


def _admin_routes() -> list[tuple[str, str]]:
    found = []
    for path, ops in app.openapi()["paths"].items():
        if path.startswith("/api/v1/admin/"):
            filled = re.sub(r"\{[a-z_]+\}", str(uuid4()), path.replace("{stream}", DLQ))
            found.extend((method.upper(), filled) for method in ops)
    return found


ROUTES = _admin_routes()


class Seen:
    """Every answer given in a test, to check that none of them carries a secret."""

    def __init__(self) -> None:
        self.bodies: list[str] = []

    def __call__(self, res: Any) -> Any:
        self.bodies.append(res.text)
        return res

    def check(self, w: World, *more: str) -> None:
        secrets = [*w.tokens.values(), *(hash_api_key(t) for t in w.tokens.values()), *more]
        for body in self.bodies:
            assert "hashed_token" not in body
            for secret in secrets:
                assert secret not in body


@pytest.fixture
def seen() -> Seen:
    return Seen()


async def test_the_admin_api_has_every_area() -> None:
    paths = {p for _, p in ROUTES}
    for area in ("keys", "users", "sessions", "connections", "audiences", "events", "dlq"):
        assert any(p.startswith(f"/api/v1/admin/{area}") for p in paths), area
    for area in ("records", "health", "audit", "review", "usage", "stats", "purge"):
        assert any(p.startswith(f"/api/v1/admin/{area}") for p in paths), area


async def test_every_admin_route_requires_admin(world: World) -> None:
    w = world
    assert len(ROUTES) > 40
    with api(w) as client:
        csrf = signed_in(client, w, "alice")
        session = client.cookies[sessions.COOKIE]
        anonymous = TestClient(app, base_url="https://testserver")
        for method, path in ROUTES:
            assert anonymous.request(method, path, json={}).status_code == 401, (method, path)
            res = client.request(
                method, path, json={}, headers={**bearer(w, "alice"), "Cookie": ""}
            )
            assert res.status_code == 403, (method, path, res.text)
            res = client.request(
                method,
                path,
                json={},
                headers={"X-CSRF-Token": csrf, "Cookie": f"{sessions.COOKIE}={session}"},
            )
            assert res.status_code == 403, (method, path, res.text)


async def test_admin_session_can_use_the_admin_api(world: World) -> None:
    w = world
    with api(w) as client:
        csrf = signed_in(client, w, "boss")
        assert client.get("/api/v1/admin/users").status_code == 200
        assert client.get("/api/v1/admin/audit").status_code == 200
        res = client.put(
            f"/api/v1/admin/users/{w.users['alice']}/admin",
            json={"is_admin": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert res.status_code == 200 and res.json()["is_admin"] is True
    [(_, key_name, key_id, user_id, _, params, _)] = await audit_rows(w, "user.admin_changed")
    assert (key_name, key_id, user_id) == ("session", None, w.users["boss"])
    assert params["is_admin"] is True


async def test_keys(world: World, seen: Seen) -> None:
    w = world
    admin, stranger = bearer(w, "admin"), bearer(w, "stranger")
    with api(w) as client:
        res = seen(
            client.post(
                "/api/v1/admin/keys",
                json={"name": "ci", "user_email": w.emails["alice"].lower()},
                headers=admin,
            )
        )
        assert res.status_code == 201
        created = res.json()
        token = created["key"]
        assert token.startswith("chk_") and created["user_email"] == w.emails["alice"].lower()
        me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
        assert me["user_id"] == str(w.users["alice"]) and me["auth"] == "key"

        listed = seen(client.get("/api/v1/admin/keys", headers=admin)).json()["items"]
        assert created["id"] in {k["id"] for k in listed}
        assert all("key" not in k for k in listed)
        bound = seen(client.get(f"/api/v1/admin/users/{w.users['alice']}", headers=admin)).json()
        assert created["id"] in {k["id"] for k in bound["keys"]}

        other = seen(client.get("/api/v1/admin/keys", headers=stranger)).json()["items"]
        assert created["id"] not in {k["id"] for k in other}
        path = f"/api/v1/admin/keys/{created['id']}/revoke"
        assert client.post(path, headers=stranger).status_code == 404
        assert (
            client.post("/api/v1/admin/keys", json={"name": ""}, headers=admin).status_code == 422
        )
        res = client.post(
            "/api/v1/admin/keys", json={"name": "x", "user_email": "no@one.test"}, headers=admin
        )
        assert res.status_code == 404

        assert seen(client.post(path, headers=admin)).status_code == 200
        res = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 401
    seen.check(w, hash_api_key(token))
    [(_, _, key_id, _, _, params, ids)] = await audit_rows(w, "key.created")
    assert key_id == w.key_ids["admin"] and token not in json.dumps(params)
    [(_, _, _, _, _, _, ids)] = await audit_rows(w, "key.revoked")
    assert [str(i) for i in ids] == [created["id"]]


async def test_people(world: World, seen: Seen) -> None:
    w = world
    admin, stranger = bearer(w, "admin"), bearer(w, "stranger")
    with api(w) as client:
        page = seen(client.get("/api/v1/admin/users?limit=2", headers=admin)).json()
        assert len(page["items"]) == 2 and page["next_cursor"]
        rest = client.get(f"/api/v1/admin/users?cursor={page['next_cursor']}", headers=admin)
        emails = {p["email"] for p in page["items"] + rest.json()["items"]}
        assert emails == {w.emails[x] for x in ("alice", "bob", "boss", "gone")}
        assert client.get("/api/v1/admin/users?cursor=nonsense", headers=admin).json() == {
            "detail": "invalid cursor"
        }
        found = client.get("/api/v1/admin/users?active=false", headers=admin).json()["items"]
        assert [p["email"] for p in found] == [w.emails["gone"]]
        found = client.get("/api/v1/admin/users?q=BOB", headers=admin).json()["items"]
        assert [p["email"] for p in found] == [w.emails["bob"]]

        alice = f"/api/v1/admin/users/{w.users['alice']}"
        assert client.get(alice, headers=stranger).status_code == 404
        assert (
            client.put(f"{alice}/admin", json={"is_admin": True}, headers=stranger).status_code
            == 404
        )
        assert client.post(f"{alice}/deactivate", headers=stranger).status_code == 404
        assert client.delete(alice, headers=stranger).status_code == 404

        res = seen(client.put(f"{alice}/admin", json={"is_admin": True}, headers=admin))
        assert res.json()["is_admin"] is True
        gone = f"/api/v1/admin/users/{w.users['gone']}"
        assert seen(client.post(f"{gone}/reactivate", headers=admin)).json()["active"] is True
        assert seen(client.post(f"{gone}/deactivate", headers=admin)).json()["active"] is False

        dry = seen(client.delete(f"{alice}?dry_run=true", headers=admin)).json()
        assert dry["dry_run"] is True
        assert client.get(alice, headers=admin).status_code == 200
        res = seen(client.delete(alice, headers=admin))
        assert res.status_code == 200 and res.json()["keys"] == 1
        assert client.get(alice, headers=admin).status_code == 404
        assert client.get("/api/v1/auth/me", headers=bearer(w, "alice")).status_code == 401
    seen.check(w)
    assert len(await audit_rows(w, "user.admin_changed")) == 1
    assert len(await audit_rows(w, "user.reactivated")) == 1
    assert len(await audit_rows(w, "user.deactivated")) == 1
    [(_, _, key_id, _, _, _, ids)] = await audit_rows(w, "user.deleted")
    assert key_id == w.key_ids["admin"] and str(w.users["alice"]) in [str(i) for i in ids]


async def test_sessions(world: World, seen: Seen) -> None:
    w = world
    admin, stranger = bearer(w, "admin"), bearer(w, "stranger")
    with api(w) as client:
        signed_in(client, w, "alice")
        token = client.cookies[sessions.COOKIE]
        signed_in(client, w, "bob")
        live = seen(client.get("/api/v1/admin/sessions", headers=admin)).json()["items"]
        assert {s["user_email"] for s in live} == {w.emails["alice"], w.emails["bob"]}
        assert client.get("/api/v1/admin/sessions", headers=stranger).json()["items"] == []
        [alices] = seen(
            client.get(f"/api/v1/admin/sessions?user_id={w.users['alice']}", headers=admin)
        ).json()["items"]
        path = f"/api/v1/admin/sessions/{alices['id']}/revoke"
        assert client.post(path, headers=stranger).status_code == 404
        assert seen(client.post(path, headers=admin)).status_code == 200
        live = client.get("/api/v1/admin/sessions", headers=admin).json()["items"]
        assert {s["user_email"] for s in live} == {w.emails["bob"]}
        ended = client.get("/api/v1/admin/sessions?include_ended=true", headers=admin).json()
        assert {s["active"] for s in ended["items"]} == {True, False}
    seen.check(w, token, hash_api_key(token), sessions.csrf_token(token))
    assert len(await audit_rows(w, "session.revoked")) == 1


async def test_connections(world: World, seen: Seen, monkeypatch: pytest.MonkeyPatch) -> None:
    w = world
    admin, stranger = bearer(w, "admin"), bearer(w, "stranger")
    client_secret = "slack-secret-" + uuid4().hex
    monkeypatch.setenv("SLACK_CLIENT_ID", "1234.5678")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", client_secret)
    monkeypatch.setenv("SLACK_REDIRECT_URI", "https://testserver/slack/oauth_redirect")
    with api(w) as client:
        body = {"connection": "jira:acme.example.net", "in_review": True}
        res = seen(client.post("/api/v1/admin/connections/review", json=body, headers=admin))
        assert res.json() == {"connection": "jira:acme.example.net", "state": "review"}
        seen(client.get("/api/v1/admin/connections", headers=admin))
        assert client.get("/api/v1/admin/connections", headers=stranger).json()["items"] == []

        res = client.post(
            "/api/v1/admin/connections/disconnect",
            json={"connection": "slack:T-none"},
            headers=admin,
        )
        assert res.status_code == 404
        res = client.post(
            "/api/v1/admin/connections/disconnect",
            json={"connection": "nope:x"},
            headers=admin,
        )
        assert res.status_code == 422

        res = seen(
            client.post(
                "/api/v1/admin/connections/install-link", json={"provider": "slack"}, headers=admin
            )
        )
        assert res.status_code == 200 and res.json()["url"].startswith("https://testserver/")
        res = client.post(
            "/api/v1/admin/connections/install-link", json={"provider": "notion"}, headers=admin
        )
        assert res.status_code == 400 and "not configured" in res.json()["detail"]
    seen.check(w, client_secret)
    [(_, _, key_id, _, _, params, _)] = await audit_rows(w, "connection.review_changed")
    assert key_id == w.key_ids["admin"]
    [(_, _, _, _, _, params, _)] = await audit_rows(w, "connections.install_link")
    assert params == {"provider": "slack"}


async def test_audiences(world: World, seen: Seen) -> None:
    w = world
    admin = bearer(w, "admin")
    with api(w) as client:
        assert seen(client.get("/api/v1/admin/audiences", headers=admin)).json()["items"] == []
        res = client.post("/api/v1/admin/audiences/refresh", json={}, headers=admin)
        assert res.status_code == 422
        res = seen(
            client.post("/api/v1/admin/audiences/refresh", json={"all": True}, headers=admin)
        )
        assert res.status_code == 200 and res.json()["items"] == []
    seen.check(w)


async def _failed_event(w: World, tenant_id: UUID) -> UUID:
    event = CanonicalEvent(
        tenant_id=tenant_id,
        source_platform=SourcePlatform.SLACK,
        source_type=SourceType.MESSAGE,
        source_id=f"C1:{uuid4().hex[:8]}",
        happened_at=datetime(2026, 3, 2, tzinfo=UTC),
        author=AuthorInfo(source_id="someone"),
        content_raw="private words in a message",
        content_clean="private words in a message",
        access_control=AccessControl(is_public=True),
        audience_container="C1",
    )
    async with tenant_context(w.engine, tenant_id) as conn:
        assert await PostgresEventStore(conn).save_event(event)
    await admin_sql(
        "UPDATE events SET pipeline_state = 'failed', pipeline_attempts = 5, "
        "pipeline_last_error = 'embedding call failed' WHERE event_id = :e",
        {"e": event.event_id},
    )
    return event.event_id


async def test_failed_events(world: World, seen: Seen) -> None:
    w = world
    admin, stranger = bearer(w, "admin"), bearer(w, "stranger")
    mine = await _failed_event(w, w.tenant_id)
    theirs = await _failed_event(w, w.other_tenant)
    with api(w) as client:
        res = seen(client.get("/api/v1/admin/events/failed", headers=admin))
        assert [e["event_id"] for e in res.json()["items"]] == [str(mine)]
        assert "private words" not in res.text
        body = {"event_ids": [str(mine), str(theirs)]}
        res = seen(client.post("/api/v1/admin/events/retry", json=body, headers=admin)).json()
        assert res == {"retried": [str(mine)], "not_failed": [str(theirs)]}
        res = client.post("/api/v1/admin/events/retry", json={"all": True}, headers=stranger)
        assert res.json()["retried"] == [str(theirs)]
        assert client.get("/api/v1/admin/events/failed", headers=admin).json()["items"] == []
    seen.check(w)
    [(_, _, key_id, _, _, _, ids)] = await audit_rows(w, "events.retry")
    assert key_id == w.key_ids["admin"] and [str(i) for i in ids] == [str(mine)]


async def test_dead_letters(world: World, seen: Seen, async_redis_client: Redis) -> None:
    dlq_redis = async_redis_client
    w = world
    admin, stranger = bearer(w, "admin"), bearer(w, "stranger")
    await dlq_redis.delete(DLQ, DLQ.removesuffix(".dlq"))
    for tenant in (w.tenant_id, w.other_tenant):
        payload = {"tenant_id": str(tenant), "event_id": str(uuid4()), "text": "private words"}
        await dead_letter.write_dead_letter(
            dlq_redis, DLQ.removesuffix(".dlq"), payload, "Traceback\nValueError: bad"
        )
    try:
        with api(w) as client:
            [stream] = [
                s
                for s in seen(client.get("/api/v1/admin/dlq", headers=admin)).json()["items"]
                if s["name"] == DLQ
            ]
            assert stream["entries"] == 1
            res = seen(client.get(f"/api/v1/admin/dlq/{DLQ}/entries", headers=admin))
            [entry] = res.json()["items"]
            assert entry["error"] == "ValueError: bad" and "private words" not in res.text
            res = client.get("/api/v1/admin/dlq/not-a-dead-letter-stream/entries", headers=admin)
            assert res.status_code == 422

            body = {"entry_ids": [entry["entry_id"]]}
            res = client.post(f"/api/v1/admin/dlq/{DLQ}/replay", json=body, headers=stranger)
            assert res.json()["done"] == []
            res = seen(client.post(f"/api/v1/admin/dlq/{DLQ}/replay", json=body, headers=admin))
            assert res.json()["done"] == [entry["entry_id"]]
            assert await dlq_redis.xlen(DLQ.removesuffix(".dlq")) == 1
            res = client.post(f"/api/v1/admin/dlq/{DLQ}/drop", json={"all": True}, headers=admin)
            assert res.json()["done"] == []
            res = client.post(f"/api/v1/admin/dlq/{DLQ}/drop", json={"all": True}, headers=stranger)
            assert len(res.json()["done"]) == 1
            assert await dlq_redis.xlen(DLQ) == 0
    finally:
        await dlq_redis.delete(DLQ, DLQ.removesuffix(".dlq"))
    seen.check(w)
    [(_, _, key_id, _, _, params, _)] = await audit_rows(w, "dlq.replay")
    assert key_id == w.key_ids["admin"] and params["stream"] == DLQ


async def test_records(world: World, seen: Seen) -> None:
    w = world
    admin, stranger = bearer(w, "admin"), bearer(w, "stranger")
    record = await seed_context(w.tenant_id, "retries stay idempotent", public=True)
    with api(w) as client:
        items = seen(client.get("/api/v1/admin/records", headers=admin)).json()["items"]
        assert [r["id"] for r in items] == [str(record)]
        assert items[0]["state"] == "live" and items[0]["severity"] == "advisory"
        assert client.get("/api/v1/admin/records", headers=stranger).json()["items"] == []
        assert client.get("/api/v1/admin/records?state=held", headers=admin).json()["items"] == []
        found = client.get("/api/v1/admin/records?q=idempotent", headers=admin).json()["items"]
        assert len(found) == 1
        assert client.get("/api/v1/admin/records?q=zzz", headers=admin).json()["items"] == []

        path = f"/api/v1/admin/records/{record}"
        detail = seen(client.get(path, headers=admin)).json()
        assert detail["summary"] == "retries stay idempotent" and detail["sources"] == []
        assert detail["scope"] == ["org"] and detail["scope_rows"] == []
        assert detail["is_locked"] is False
        scope = {
            "scope_rows": [{"scope_type": "repo", "scope_value": "acme/api"}],
            "is_locked": True,
        }
        assert seen(client.patch(f"{path}/scope", json=scope, headers=admin)).status_code == 200
        detail = client.get(path, headers=admin).json()
        assert detail["scope"] == ["repo:acme/api"] and detail["is_locked"] is True
        assert detail["scope_rows"] == scope["scope_rows"]
        assert client.get(path, headers=stranger).status_code == 404
        assert client.delete(path, headers=stranger).status_code == 404

        res = seen(client.patch(f"{path}/severity", json={"severity": "blocking"}, headers=admin))
        assert res.status_code == 200
        assert client.get(path, headers=admin).json()["severity"] == "blocking"

        assert seen(client.delete(f"{path}?dry_run=true", headers=admin)).json()["dry_run"] is True
        res = seen(client.delete(path, headers=admin))
        assert res.status_code == 200 and res.json()["records_deleted"] == 1
        assert client.get(path, headers=admin).status_code == 404
    seen.check(w)
    assert len(await audit_rows(w, "records.search")) >= 1
    [(_, _, key_id, _, _, _, _)] = await audit_rows(w, "data.purged")
    assert key_id == w.key_ids["admin"]


async def test_health(world: World, seen: Seen) -> None:
    w = world
    with api(w) as client:
        res = seen(client.get("/api/v1/admin/health", headers=bearer(w, "admin")))
        assert res.status_code == 200
        checks = {c["name"]: c for c in res.json()["checks"]}
        assert checks["sign-in"]["status"] in ("PASS", "WARN")
        assert "database" in checks
    seen.check(w, "app-role-test")


async def test_openapi_lists_the_admin_api(world: World) -> None:
    w = world
    with api(w) as client:
        spec = client.get("/openapi.json").json()
    tags = {t for path in spec["paths"].values() for op in path.values() for t in op["tags"]}
    assert {"sign-in", "admin: keys", "admin: people", "admin: sessions"} <= tags
    op = spec["paths"]["/api/v1/admin/keys"]["post"]
    assert op["summary"] == "Create an API key"
    assert "201" in op["responses"] and "403" in op["responses"]
