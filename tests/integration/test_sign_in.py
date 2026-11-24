"""Browser sign-in against the fake identity provider, and what a session may do."""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from key_helpers import seed_context
from sign_in_helpers import (
    WEB,
    World,
    admin_sql,
    api,
    audit_rows,
    bearer,
    sign_in,
    signed_in,
    world,
)

from checkcord.core.config import get_settings
from checkcord.services import sessions

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

__all__ = ["world"]


def _with(token: str) -> dict[str, str]:
    return {"Cookie": f"{sessions.COOKIE}={token}"}


def _cookie_header(res: Any, name: str) -> str:
    [header] = [h for h in res.headers.get_list("set-cookie") if h.startswith(f"{name}=")]
    return header.lower()


async def test_full_sign_in_flow(world: World) -> None:
    w = world
    with api(w) as client:
        start = client.get("/api/v1/auth/login", follow_redirects=False)
        assert start.status_code == 302
        query = parse_qs(urlsplit(start.headers["location"]).query)
        assert query["code_challenge_method"] == ["S256"] and query["nonce"][0]
        assert query["scope"] == ["openid email profile"]
        login_cookie = _cookie_header(start, "checkcord_login")
        assert "httponly" in login_cookie and "path=/api/v1/auth" in login_cookie

        w.idp.email = w.emails["alice"].lower()
        authorize = TestClient(w.idp.app).get(start.headers["location"], follow_redirects=False)
        done = client.get(authorize.headers["location"], follow_redirects=False)
        assert done.status_code == 303 and done.headers["location"] == WEB
        cookie = _cookie_header(done, sessions.COOKIE)
        for attribute in ("httponly", "secure", "samesite=lax", "path=/"):
            assert attribute in cookie

        res = client.get("/api/v1/auth/me")
        assert res.status_code == 200, res.text
        me = res.json()
        assert me["auth"] == "session" and me["email"] == w.emails["alice"]
        assert me["user_id"] == str(w.users["alice"]) and me["is_admin"] is False
        assert me["csrf_header"] == "X-CSRF-Token" and me["csrf_token"]
        assert me["key_id"] is None

        by_key = client.get("/api/v1/auth/me", headers=bearer(w, "alice")).json()
        assert by_key["auth"] == "key" and by_key["csrf_token"] is None

    [(channel, key_name, key_id, user_id, email, params, _)] = await audit_rows(w, "auth.signed_in")
    assert (channel, key_name, key_id) == ("api", "session", None)
    assert user_id == w.users["alice"] and email == w.emails["alice"]
    assert "token" not in str(params).lower()


async def test_return_to_must_be_an_allowed_origin(world: World) -> None:
    w = world
    with api(w) as client:
        res = client.get(
            "/api/v1/auth/login",
            params={"return_to": "https://evil.test/x"},
            follow_redirects=False,
        )
        assert res.status_code == 400
        done = sign_in(client, w, w.emails["alice"], return_to=f"{WEB}/graph?x=1")
        assert done.status_code == 303 and done.headers["location"] == f"{WEB}/graph?x=1"


REFUSALS: list[tuple[str, dict[str, Any], str | None, str]] = [
    ("unverified email", {"claims": {"email_verified": False}}, "alice", "unverified_email"),
    ("unknown person", {}, None, "unknown_person"),
    ("deactivated person", {}, "gone", "deactivated_person"),
    ("wrong audience", {"audience": "another-client"}, "alice", "invalid_token"),
    ("wrong nonce", {"nonce": "not-the-one-sent"}, "alice", "invalid_token"),
    ("expired token", {"lifetime_seconds": -600}, "alice", "invalid_token"),
    ("bad signature", {"sign_with_unknown_key": True}, "alice", "invalid_token"),
]


def refused(res: Any, code: str) -> bool:
    return res.status_code == 303 and res.headers["location"] == f"{WEB}/sign-in?error={code}"


@pytest.mark.parametrize(
    ("knobs", "who", "reason"),
    [r[1:] for r in REFUSALS],
    ids=[r[0] for r in REFUSALS],
)
async def test_sign_in_refusals(
    world: World, knobs: dict[str, Any], who: str | None, reason: str
) -> None:
    w = world
    for name, value in knobs.items():
        setattr(w.idp, name, value)
    email = w.emails[who] if who else "nobody@elsewhere.test"
    with api(w) as client:
        res = sign_in(client, w, email)
        assert refused(res, reason), res.headers
        assert sessions.COOKIE not in res.cookies
        assert client.get("/api/v1/auth/me").status_code == 401
    [(_, key_name, _, _, _, params, _)] = await audit_rows(w, "auth.sign_in_refused")
    assert key_name == "session" and params["reason"] == reason
    assert not await admin_sql(
        "SELECT 1 FROM user_sessions WHERE tenant_id = :t", {"t": w.tenant_id}
    )


async def test_replayed_or_foreign_state_is_refused(world: World) -> None:
    w = world
    with api(w) as client:
        start = client.get("/api/v1/auth/login", follow_redirects=False)
        w.idp.email = w.emails["alice"]
        callback = TestClient(w.idp.app).get(start.headers["location"], follow_redirects=False)
        url = callback.headers["location"]
        assert client.get(url, follow_redirects=False).status_code == 303
        replay = client.get(url, follow_redirects=False)
        assert refused(replay, "bad_state")

        start = client.get("/api/v1/auth/login", follow_redirects=False)
        callback = TestClient(w.idp.app).get(start.headers["location"], follow_redirects=False)
        with TestClient(client.app, base_url="https://testserver") as stranger:
            res = stranger.get(callback.headers["location"], follow_redirects=False)
            assert refused(res, "bad_state")
    assert len(await audit_rows(w, "auth.sign_in_refused")) == 1
    assert w.idp.tokens_issued == 1


async def test_a_provider_error_goes_to_the_sign_in_page(world: World) -> None:
    w = world
    with api(w) as client:
        start = client.get("/api/v1/auth/login", follow_redirects=False)
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        res = client.get(
            "/api/v1/auth/callback",
            params={"state": state, "error": "access_denied"},
            follow_redirects=False,
        )
        assert refused(res, "provider_error")
    [(_, _, _, _, _, params, _)] = await audit_rows(w, "auth.sign_in_refused")
    assert params["reason"] == "provider_error"


async def test_group_claim_drives_the_admin_flag(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    monkeypatch.setenv("OIDC_ADMIN_GROUPS_CLAIM", "groups")
    monkeypatch.setenv("OIDC_ADMIN_GROUPS", "checkcord-admins,platform")
    with api(w) as client:
        w.idp.groups = ["engineering", "checkcord-admins"]
        signed_in(client, w, "alice")
        assert client.get("/api/v1/auth/me").json()["is_admin"] is True
        assert client.get("/api/v1/admin/users").status_code == 200

        w.idp.groups = ["engineering"]
        signed_in(client, w, "alice")
        assert client.get("/api/v1/auth/me").json()["is_admin"] is False
        assert client.get("/api/v1/admin/users").status_code == 403

        w.idp.groups = []
        signed_in(client, w, "boss")
        assert client.get("/api/v1/auth/me").json()["is_admin"] is False

    changes = await audit_rows(w, "user.admin_changed")
    assert [(r[5]["is_admin"], r[5]["source"]) for r in changes] == [
        (True, "groups_claim"),
        (False, "groups_claim"),
        (False, "groups_claim"),
    ]


async def test_scim_admin_groups_take_precedence_over_the_claim(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    monkeypatch.setenv("OIDC_ADMIN_GROUPS_CLAIM", "groups")
    monkeypatch.setenv("OIDC_ADMIN_GROUPS", "checkcord-admins")
    monkeypatch.setenv("SCIM_ADMIN_GROUPS", "Checkcord Admins")
    get_settings.cache_clear()
    with api(w) as client:
        w.idp.groups = ["checkcord-admins"]
        signed_in(client, w, "alice")
        assert client.get("/api/v1/auth/me").json()["is_admin"] is False

        res = client.post(
            "/scim/v2/Groups",
            json={"displayName": "Checkcord Admins", "members": [{"value": str(w.users["alice"])}]},
            headers=bearer(w, "admin"),
        )
        assert res.status_code == 201, res.text
        w.idp.groups = []
        signed_in(client, w, "alice")
        assert client.get("/api/v1/auth/me").json()["is_admin"] is True

        removed = client.patch(
            f"/scim/v2/Groups/{res.json()['id']}",
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "remove", "path": f'members[value eq "{w.users["alice"]}"]'}],
            },
            headers=bearer(w, "admin"),
        )
        assert removed.status_code == 204, removed.text
        assert client.get("/api/v1/auth/me").json()["is_admin"] is False
        assert client.get("/api/v1/admin/users").status_code == 403

        w.idp.groups = ["checkcord-admins"]
        signed_in(client, w, "boss")
        assert client.get("/api/v1/auth/me").json()["is_admin"] is False
        assert client.get("/api/v1/admin/users").status_code == 403

    changes = await audit_rows(w, "user.admin_changed")
    assert [(r[5]["is_admin"], r[5]["source"]) for r in changes] == [
        (True, "scim_group"),
        (False, "scim_group"),
        (False, "scim_group"),
    ]


async def test_admin_flag_comes_from_the_person_without_a_group_claim(world: World) -> None:
    w = world
    with api(w) as client:
        w.idp.groups = ["checkcord-admins"]
        signed_in(client, w, "alice")
        assert client.get("/api/v1/auth/me").json()["is_admin"] is False
        signed_in(client, w, "boss")
        assert client.get("/api/v1/auth/me").json()["is_admin"] is True


async def test_auto_create_only_for_listed_domains(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    monkeypatch.setenv("OIDC_AUTO_CREATE_DOMAINS", "newco.test")
    with api(w) as client:
        assert sign_in(client, w, "Fresh@NewCo.test").status_code == 303
        assert client.get("/api/v1/auth/me").json()["email"] == "Fresh@NewCo.test"
        assert refused(sign_in(client, w, "someone@notlisted.test"), "unknown_person")
    [(source,)] = await admin_sql(
        "SELECT params->>'source' FROM audit_log_entries WHERE tenant_id = :t "
        "AND action = 'user.created'",
        {"t": w.tenant_id},
    )
    assert source == "sign_in"


async def test_session_reads_what_the_persons_key_reads(world: World) -> None:
    w = world
    private = await seed_context(
        w.tenant_id, "alice only", public=False, allowed=[w.users["alice"]]
    )
    public = await seed_context(w.tenant_id, "everyone", public=True)
    path = "/api/v1/graph/node/context:{}?repo_identifier=acme/x"

    def reads(client: TestClient, headers: dict[str, str]) -> dict[Any, Any]:
        return {
            r: (res.status_code, res.json() if res.status_code == 200 else None)
            for r in (private, public)
            for res in [client.get(path.format(r), headers=headers)]
        }

    with api(w) as client:
        signed_in(client, w, "alice")
        alice = client.cookies[sessions.COOKIE]
        signed_in(client, w, "bob")
        bob = client.cookies[sessions.COOKIE]
        alice_key = reads(client, bearer(w, "alice"))
        assert reads(client, _with(alice)) == alice_key
        assert alice_key[private][0] == 200 and alice_key[public][0] == 200
        bob_key = reads(client, bearer(w, "bob"))
        assert reads(client, _with(bob)) == bob_key
        assert bob_key[private][0] == 404 and bob_key[public][0] == 200


async def test_ordinary_session_is_refused_admin_routes(world: World) -> None:
    w = world
    with api(w) as client:
        csrf = signed_in(client, w, "alice")
        res = client.get("/api/v1/admin/users")
        assert res.status_code == 403 and res.json() == {
            "detail": "Forbidden: admin rights required"
        }
        res = client.post("/api/v1/admin/keys", json={"name": "x"}, headers={"X-CSRF-Token": csrf})
        assert res.status_code == 403


async def test_state_change_needs_the_csrf_header(world: World) -> None:
    w = world
    with api(w) as client:
        csrf = signed_in(client, w, "boss")
        body = {"name": "made in the browser"}
        assert client.post("/api/v1/admin/keys", json=body).status_code == 403
        wrong = {"X-CSRF-Token": "x" + csrf[1:]}
        res = client.post("/api/v1/admin/keys", json=body, headers=wrong)
        assert res.status_code == 403 and "CSRF" in res.json()["detail"]
        assert client.get("/api/v1/admin/keys").status_code == 200
        res = client.post("/api/v1/admin/keys", json=body, headers={"X-CSRF-Token": csrf})
        assert res.status_code == 201

        with TestClient(client.app, base_url="https://testserver") as by_key:
            res = by_key.post("/api/v1/admin/keys", json=body, headers=bearer(w, "admin"))
            assert res.status_code == 201

    [(_, key_name, key_id, user_id, _, _, _), (_, _, admin_key, _, _, _, _)] = await audit_rows(
        w, "key.created"
    )
    assert (key_name, key_id, user_id) == ("session", None, w.users["boss"])
    assert admin_key == w.key_ids["admin"]


async def test_logout_ends_the_session_at_once(world: World) -> None:
    w = world
    with api(w) as client:
        csrf = signed_in(client, w, "alice")
        token = client.cookies[sessions.COOKIE]
        assert client.post("/api/v1/auth/logout").status_code == 403
        res = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})
        assert res.status_code == 204
        assert sessions.COOKIE not in client.cookies
        assert client.get("/api/v1/auth/me", headers=_with(token)).status_code == 401
    [(_, key_name, _, user_id, _, _, _)] = await audit_rows(w, "auth.signed_out")
    assert key_name == "session" and user_id == w.users["alice"]


async def test_deactivation_ends_sessions_and_keys_at_once(world: World) -> None:
    w = world
    with api(w) as client:
        signed_in(client, w, "alice")
        assert client.get("/api/v1/auth/me", headers=bearer(w, "alice")).status_code == 200
        res = client.post(
            f"/api/v1/admin/users/{w.users['alice']}/deactivate", headers=bearer(w, "admin")
        )
        assert res.status_code == 200 and res.json()["sessions_revoked"] == 1
        assert client.get("/api/v1/auth/me").status_code == 401
        assert client.get("/api/v1/auth/me", headers=bearer(w, "alice")).status_code == 401
        assert refused(sign_in(client, w, w.emails["alice"]), "deactivated_person")

        signed_in(client, w, "bob")
        res = client.patch(
            f"/scim/v2/Users/{w.users['bob']}",
            json={"Operations": [{"op": "replace", "path": "active", "value": False}]},
            headers=bearer(w, "admin"),
        )
        assert res.status_code == 200, res.text
        assert client.get("/api/v1/auth/me").status_code == 401
    rows = await audit_rows(w, "user.deactivated")
    assert [(r[2], r[5]["sessions_revoked"]) for r in rows] == [
        (w.key_ids["admin"], 1),
        (w.key_ids["admin"], 1),
    ]


async def test_deleting_a_person_ends_their_session(world: World) -> None:
    w = world
    with api(w) as client:
        signed_in(client, w, "alice")
        res = client.delete(f"/api/v1/admin/users/{w.users['alice']}", headers=bearer(w, "admin"))
        assert res.status_code == 200, res.text
        assert client.get("/api/v1/auth/me").status_code == 401


@pytest.mark.parametrize("column", ["last_seen_at", "expires_at"])
async def test_idle_and_absolute_lifetimes(world: World, column: str) -> None:
    w = world
    with api(w) as client:
        signed_in(client, w, "alice")
        past = datetime.now(UTC) - timedelta(hours=48)
        await admin_sql(
            f"UPDATE user_sessions SET {column} = :p WHERE tenant_id = :t",
            {"p": past, "t": w.tenant_id},
        )
        assert client.get("/api/v1/auth/me").status_code == 401


async def test_admin_revokes_a_session(world: World) -> None:
    w = world
    with api(w) as client:
        signed_in(client, w, "alice")
        listed = client.get("/api/v1/admin/sessions", headers=bearer(w, "admin")).json()
        [session] = listed["items"]
        assert session["user_email"] == w.emails["alice"] and session["active"] is True
        res = client.post(
            f"/api/v1/admin/sessions/{session['id']}/revoke", headers=bearer(w, "admin")
        )
        assert res.status_code == 200
        assert client.get("/api/v1/auth/me").status_code == 401
        res = client.post(
            f"/api/v1/admin/sessions/{session['id']}/revoke", headers=bearer(w, "admin")
        )
        assert res.status_code == 404
    [(_, _, key_id, _, _, _, record_ids)] = await audit_rows(w, "session.revoked")
    assert key_id == w.key_ids["admin"] and [str(r) for r in record_ids] == [session["id"]]


async def test_without_a_provider_the_cookie_is_ignored(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    with api(w) as client:
        signed_in(client, w, "alice")
        token = client.cookies[sessions.COOKIE]
    monkeypatch.delenv("OIDC_ISSUER")
    get_settings.cache_clear()
    with TestClient(client.app, base_url="https://testserver") as plain:
        assert plain.get("/api/v1/auth/me", headers=_with(token)).status_code == 401
        assert plain.get("/api/v1/auth/login", follow_redirects=False).status_code == 404
        assert plain.get("/api/v1/auth/me", headers=bearer(w, "alice")).status_code == 200
