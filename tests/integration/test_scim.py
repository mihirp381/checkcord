"""SCIM against recorded Okta and Microsoft Entra request shapes, through the app role (RLS)."""

from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app_role_helpers import app_database_url, create_app_role, drop_app_role
from fastapi.testclient import TestClient
from key_helpers import KeySpec, seeded_keys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.core.config import get_settings
from checkcord.main import app

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ADMIN_URL = get_settings().database_url
S = "/scim/v2"
PATCH_OP = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
USER = "urn:ietf:params:scim:schemas:core:2.0:User"
ENTERPRISE = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
TABLES = ("scim_groups", "audit_log_entries", "user_sessions", "enterprise_users")


@dataclass
class World:
    tenant_id: UUID
    other_tenant: UUID
    tokens: dict[str, str]
    domain: str

    def auth(self, who: str = "admin") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.tokens[who]}",
            "Content-Type": "application/scim+json; charset=utf-8",
        }


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
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[World, None]:
    tenant_id, other = uuid4(), uuid4()
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://checkcord.example.test")
    monkeypatch.delenv("SCIM_ADMIN_GROUPS", raising=False)
    get_settings.cache_clear()
    tokens = {who: f"scim_{who}_{uuid4().hex}" for who in ("admin", "user", "other_admin")}
    specs = [
        KeySpec(tokens["admin"], tenant_id, admin=True),
        KeySpec(tokens["user"], tenant_id),
        KeySpec(tokens["other_admin"], other, admin=True),
    ]
    async with seeded_keys(specs):
        role, url = await create_app_role()
        try:
            with app_database_url(url):
                yield World(tenant_id, other, tokens, f"d{tenant_id.hex[:8]}.example.com")
        finally:
            await drop_app_role(role)
            for table in TABLES:
                await _admin(
                    f"DELETE FROM {table} WHERE tenant_id IN (:t, :o)", {"t": tenant_id, "o": other}
                )
            get_settings.cache_clear()


@contextmanager
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def okta_user(w: World, login: str, active: bool = True) -> dict[str, Any]:
    return {
        "schemas": [USER],
        "userName": f"{login}@{w.domain}",
        "name": {"givenName": login.capitalize(), "familyName": "Jensen"},
        "emails": [{"primary": True, "value": f"{login}@{w.domain}", "type": "work"}],
        "displayName": f"{login.capitalize()} Jensen",
        "locale": "en-US",
        "externalId": f"00u{login}okta",
        "groups": [],
        "password": "never-stored-" + uuid4().hex,
        "active": active,
    }


def entra_user(w: World, login: str) -> dict[str, Any]:
    return {
        "schemas": [USER, ENTERPRISE],
        "externalId": f"{login}-entra-object-id",
        "userName": f"{login}@{w.domain}",
        "active": True,
        "emails": [{"primary": True, "type": "work", "value": f"{login}@{w.domain}"}],
        "meta": {"resourceType": "User"},
        "name": {"formatted": f"{login} Smith", "familyName": "Smith", "givenName": login},
        "roles": [],
        ENTERPRISE: {"department": "Payments"},
    }


async def _audit(w: World, prefix: str) -> list[Any]:
    return await _admin(
        "SELECT action, params, channel FROM audit_log_entries "
        "WHERE tenant_id = :t AND action LIKE :p ORDER BY created_at, id",
        {"t": w.tenant_id, "p": f"{prefix}%"},
    )


async def test_discovery_endpoints(world: World) -> None:
    w = world
    with client() as c:
        config = c.get(f"{S}/ServiceProviderConfig", headers=w.auth())
        assert config.status_code == 200
        assert config.headers["content-type"].startswith("application/scim+json")
        body = config.json()
        assert body["patch"]["supported"] is True and body["bulk"]["supported"] is False
        assert body["filter"] == {"supported": True, "maxResults": 200}
        assert body["authenticationSchemes"][0]["type"] == "oauthbearertoken"

        types = c.get(f"{S}/ResourceTypes", headers=w.auth()).json()
        assert [t["id"] for t in types["Resources"]] == ["User", "Group"]
        assert types["Resources"][0]["schemaExtensions"] == [
            {"schema": ENTERPRISE, "required": False}
        ]
        assert c.get(f"{S}/ResourceTypes/Group", headers=w.auth()).json()["endpoint"] == "/Groups"

        schemas = c.get(f"{S}/Schemas", headers=w.auth()).json()
        assert {s["id"] for s in schemas["Resources"]} == {USER, GROUP, ENTERPRISE}
        user = c.get(f"{S}/Schemas/{USER}", headers=w.auth()).json()
        assert "userName" in [a["name"] for a in user["attributes"]]
        missing = c.get(f"{S}/Schemas/urn:nope", headers=w.auth())
        assert missing.status_code == 404 and missing.json()["status"] == "404"

        refused = c.get(f"{S}/ServiceProviderConfig", headers=w.auth("user"))
        assert refused.status_code == 403
        assert refused.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]


async def test_okta_user_lifecycle(world: World) -> None:
    w = world
    with client() as c:
        email = f"bjensen@{w.domain}"
        probe = c.get(
            f'{S}/Users?filter=userName eq "{email}"&startIndex=1&count=100', headers=w.auth()
        )
        assert probe.status_code == 200
        assert probe.json() == {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
            "totalResults": 0,
            "startIndex": 1,
            "itemsPerPage": 0,
            "Resources": [],
        }

        created = c.post(f"{S}/Users", json=okta_user(w, "bjensen"), headers=w.auth())
        assert created.status_code == 201, created.text
        user = created.json()
        user_id = user["id"]
        assert (
            created.headers["location"] == f"https://checkcord.example.test/scim/v2/Users/{user_id}"
        )
        assert user["userName"] == email and user["active"] is True
        assert user["externalId"] == "00ubjensenokta" and user["locale"] == "en-US"
        assert "password" not in user and "groups" not in user
        stored = await _admin(
            "SELECT scim_metadata FROM enterprise_users WHERE id = :i", {"i": user_id}
        )
        assert "password" not in stored[0][0]

        dup = c.post(f"{S}/Users", json=okta_user(w, "bjensen"), headers=w.auth())
        assert dup.status_code == 409
        assert dup.json()["scimType"] == "uniqueness" and dup.json()["status"] == "409"

        found = c.get(
            f'{S}/Users?filter=userName eq "BJensen@{w.domain.upper()}"', headers=w.auth()
        ).json()
        assert found["totalResults"] == 1 and found["Resources"][0]["id"] == user_id

        update = okta_user(w, "bjensen")
        update["name"]["givenName"] = "Barbara"
        update["displayName"] = "Barbara Jensen"
        put = c.put(f"{S}/Users/{user_id}", json=update, headers=w.auth())
        assert put.status_code == 200, put.text
        assert put.json()["displayName"] == "Barbara Jensen"

        off = c.patch(
            f"{S}/Users/{user_id}",
            json={
                "schemas": [PATCH_OP],
                "Operations": [{"op": "replace", "value": {"active": False}}],
            },
            headers=w.auth(),
        )
        assert off.status_code == 200 and off.json()["active"] is False
        on = c.patch(
            f"{S}/Users/{user_id}",
            json={
                "schemas": [PATCH_OP],
                "Operations": [{"op": "replace", "value": {"active": True}}],
            },
            headers=w.auth(),
        )
        assert on.json()["active"] is True

        assert c.delete(f"{S}/Users/{user_id}", headers=w.auth()).status_code == 204
        gone = c.get(f"{S}/Users/{user_id}", headers=w.auth())
        assert gone.status_code == 404 and gone.json()["detail"] == "User not found"

    actions = [(a, p.get("active"), ch) for a, p, ch in await _audit(w, "user.")]
    assert actions == [
        ("user.created", True, "scim"),
        ("user.updated", True, "scim"),
        ("user.deactivated", False, "scim"),
        ("user.updated", True, "scim"),
        ("user.deleted", None, "scim"),
    ]


async def test_entra_user_shapes(world: World) -> None:
    w = world
    with client() as c:
        probe = c.get(
            f'{S}/Users?filter=userName eq "x-{uuid4().hex}@{w.domain}"', headers=w.auth()
        )
        assert probe.json()["totalResults"] == 0
        created = c.post(f"{S}/Users", json=entra_user(w, "alex"), headers=w.auth())
        assert created.status_code == 201, created.text
        user_id = created.json()["id"]
        assert created.json()[ENTERPRISE] == {"department": "Payments"}
        assert ENTERPRISE in created.json()["schemas"]

        by_external = c.get(
            f'{S}/Users?filter=externalId eq "alex-entra-object-id"', headers=w.auth()
        ).json()
        assert [u["id"] for u in by_external["Resources"]] == [user_id]
        assert (
            c.get(
                f'{S}/Users?filter=externalId eq "ALEX-ENTRA-OBJECT-ID"', headers=w.auth()
            ).json()["totalResults"]
            == 0
        )

        changes = {
            "schemas": [PATCH_OP],
            "Operations": [
                {"op": "Replace", "path": "displayName", "value": "Alex Smith-Jones"},
                {"op": "Add", "path": "name.familyName", "value": "Smith-Jones"},
                {
                    "op": "Replace",
                    "path": 'emails[type eq "work"].value',
                    "value": f"alex.sj@{w.domain}",
                },
                {"op": "Add", "path": f"{ENTERPRISE}:department", "value": "Platform"},
                {"op": "Replace", "path": "TITLE", "value": "Engineer"},
            ],
        }
        patched = c.patch(f"{S}/Users/{user_id}", json=changes, headers=w.auth())
        assert patched.status_code == 200, patched.text
        body = patched.json()
        assert body["displayName"] == "Alex Smith-Jones"
        assert body["name"]["familyName"] == "Smith-Jones"
        assert body["emails"] == [{"primary": True, "type": "work", "value": f"alex.sj@{w.domain}"}]
        assert body[ENTERPRISE]["department"] == "Platform" and body["title"] == "Engineer"
        row = await _admin(
            "SELECT primary_email, display_name FROM enterprise_users WHERE id = :i", {"i": user_id}
        )
        assert tuple(row[0]) == (f"alex.sj@{w.domain}", "Alex Smith-Jones")

        no_path = {
            "schemas": [PATCH_OP],
            "Operations": [
                {"op": "replace", "value": {"name.givenName": "Alexandra", "externalId": "new-oid"}}
            ],
        }
        body = c.patch(f"{S}/Users/{user_id}", json=no_path, headers=w.auth()).json()
        assert body["name"]["givenName"] == "Alexandra" and body["externalId"] == "new-oid"

        disable = {
            "schemas": [PATCH_OP],
            "Operations": [{"op": "Replace", "path": "active", "value": "False"}],
        }
        body = c.patch(f"{S}/Users/{user_id}", json=disable, headers=w.auth()).json()
        assert body["active"] is False
        active = await _admin("SELECT active FROM enterprise_users WHERE id = :i", {"i": user_id})
        assert active[0][0] is False

        bad = {
            "schemas": [PATCH_OP],
            "Operations": [{"op": "Replace", "path": "active", "value": "maybe"}],
        }
        res = c.patch(f"{S}/Users/{user_id}", json=bad, headers=w.auth())
        assert res.status_code == 400 and res.json()["scimType"] == "invalidValue"


async def test_case_insensitive_names_and_errors(world: World) -> None:
    w = world
    with client() as c:
        shouty = {
            "SCHEMAS": [USER],
            "USERNAME": f"case@{w.domain}",
            "Emails": [{"VALUE": f"case@{w.domain}", "Primary": "true"}],
            "ACTIVE": True,
        }
        created = c.post(f"{S}/Users", json=shouty, headers=w.auth())
        assert created.status_code == 201, created.text
        assert created.json()["userName"] == f"case@{w.domain}"
        assert created.json()["emails"] == [{"value": f"case@{w.domain}", "primary": True}]
        listed = c.get(f'{S}/Users?FILTER=USERNAME eq "CASE@{w.domain}"', headers=w.auth())
        assert listed.json()["totalResults"] == 1

        cases = [
            (c.get(f'{S}/Users?filter=userName sw "case"', headers=w.auth()), 400, "invalidFilter"),
            (c.get(f'{S}/Users?filter=title eq "x"', headers=w.auth()), 400, "invalidFilter"),
            (c.get(f"{S}/Users?startIndex=abc", headers=w.auth()), 400, "invalidValue"),
            (c.post(f"{S}/Users", content=b"{not json", headers=w.auth()), 400, "invalidSyntax"),
            (c.post(f"{S}/Users", json={"active": True}, headers=w.auth()), 400, "invalidValue"),
            (
                c.post(f"{S}/Users", json={"userName": "nomail"}, headers=w.auth()),
                400,
                "invalidValue",
            ),
            (
                c.patch(
                    f"{S}/Users/{created.json()['id']}", json={"Operations": []}, headers=w.auth()
                ),
                400,
                "invalidSyntax",
            ),
            (
                c.patch(
                    f"{S}/Users/{created.json()['id']}",
                    json={"Operations": [{"op": "move", "path": "active"}]},
                    headers=w.auth(),
                ),
                400,
                "invalidSyntax",
            ),
        ]
        for res, status, scim_type in cases:
            assert res.status_code == status, res.text
            assert res.json()["scimType"] == scim_type
            assert res.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
        assert c.get(f"{S}/Users/not-a-uuid", headers=w.auth()).status_code == 404
        assert c.post(f"{S}/Users", json={}, headers={}).status_code == 401


async def test_pagination(world: World) -> None:
    w = world
    with client() as c:
        for i in range(5):
            res = c.post(f"{S}/Users", json=okta_user(w, f"p{i}"), headers=w.auth())
            assert res.status_code == 201
        first = c.get(f"{S}/Users?startIndex=1&count=2", headers=w.auth()).json()
        assert (first["totalResults"], first["itemsPerPage"], first["startIndex"]) == (5, 2, 1)
        rest = c.get(f"{S}/Users?startIndex=3&count=10", headers=w.auth()).json()
        assert rest["itemsPerPage"] == 3
        seen = [u["userName"] for u in first["Resources"] + rest["Resources"]]
        assert sorted(seen) == sorted(f"p{i}@{w.domain}" for i in range(5))
        assert c.get(f"{S}/Users?count=0", headers=w.auth()).json()["Resources"] == []
        assert c.get(f"{S}/Users?startIndex=0", headers=w.auth()).json()["startIndex"] == 1


def _people(c: TestClient, w: World, *logins: str) -> list[str]:
    ids = []
    for login in logins:
        res = c.post(f"{S}/Users", json=okta_user(w, login), headers=w.auth())
        assert res.status_code == 201, res.text
        ids.append(res.json()["id"])
    return ids


async def _flags(w: World) -> dict[str, bool]:
    rows = await _admin(
        "SELECT primary_email, is_admin FROM enterprise_users WHERE tenant_id = :t",
        {"t": w.tenant_id},
    )
    return {email.split("@")[0]: is_admin for email, is_admin in rows}


async def test_okta_group_push(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    w = world
    monkeypatch.setenv("SCIM_ADMIN_GROUPS", "Checkcord Admins")
    get_settings.cache_clear()
    with client() as c:
        ann, ben, cat = _people(c, w, "ann", "ben", "cat")
        probe = c.get(f'{S}/Groups?filter=displayName eq "Checkcord Admins"', headers=w.auth())
        assert probe.json()["totalResults"] == 0
        created = c.post(
            f"{S}/Groups",
            json={
                "schemas": [GROUP],
                "displayName": "Checkcord Admins",
                "members": [{"value": ann, "display": "ann"}, {"value": str(uuid4())}],
            },
            headers=w.auth(),
        )
        assert created.status_code == 201, created.text
        group_id = created.json()["id"]
        assert [m["value"] for m in created.json()["members"]] == [ann]
        assert await _flags(w) == {"ann": True, "ben": False, "cat": False}

        dup = c.post(f"{S}/Groups", json={"displayName": "checkcord admins"}, headers=w.auth())
        assert dup.status_code == 409 and dup.json()["scimType"] == "uniqueness"

        add = {
            "schemas": [PATCH_OP],
            "Operations": [
                {"op": "add", "path": "members", "value": [{"value": ben, "display": "ben"}]}
            ],
        }
        assert c.patch(f"{S}/Groups/{group_id}", json=add, headers=w.auth()).status_code == 204
        remove = {
            "schemas": [PATCH_OP],
            "Operations": [{"op": "remove", "path": f'members[value eq "{ann}"]'}],
        }
        assert c.patch(f"{S}/Groups/{group_id}", json=remove, headers=w.auth()).status_code == 204
        assert await _flags(w) == {"ann": False, "ben": True, "cat": False}

        found = c.get(
            f'{S}/Groups?filter=displayName eq "CHECKCORD ADMINS"', headers=w.auth()
        ).json()
        assert [m["value"] for m in found["Resources"][0]["members"]] == [ben]

        rename = {
            "schemas": [PATCH_OP],
            "Operations": [
                {"op": "replace", "value": {"id": group_id, "displayName": "Former Admins"}}
            ],
        }
        assert c.patch(f"{S}/Groups/{group_id}", json=rename, headers=w.auth()).status_code == 204
        assert await _flags(w) == {"ann": False, "ben": False, "cat": False}

        put = c.put(
            f"{S}/Groups/{group_id}",
            json={
                "schemas": [GROUP],
                "displayName": "Checkcord Admins",
                "members": [{"value": cat}],
            },
            headers=w.auth(),
        )
        assert put.status_code == 200 and [m["value"] for m in put.json()["members"]] == [cat]
        assert await _flags(w) == {"ann": False, "ben": False, "cat": True}

        assert c.delete(f"{S}/Groups/{group_id}", headers=w.auth()).status_code == 204
        assert c.get(f"{S}/Groups/{group_id}", headers=w.auth()).status_code == 404
        assert await _flags(w) == {"ann": False, "ben": False, "cat": False}

    entries = await _audit(w, "group.")
    assert [(a, p.get("added"), p.get("removed")) for a, p, _ in entries] == [
        ("group.created", 1, 0),
        ("group.updated", 1, 0),
        ("group.updated", 0, 1),
        ("group.updated", 0, 0),
        ("group.updated", 1, 1),
        ("group.deleted", None, None),
    ]
    assert entries[0][1]["ignored_members"] == 1
    assert entries[3][1]["renamed_from"] == "Checkcord Admins"
    sources = [p["source"] for _, p, _ in await _audit(w, "user.admin_changed")]
    assert set(sources) == {"scim_group"} and len(sources) == 6


async def test_entra_group_shapes_and_isolation(world: World) -> None:
    w = world
    with client() as c:
        dee, eve = _people(c, w, "dee", "eve")
        created = c.post(
            f"{S}/Groups",
            json={
                "schemas": [GROUP],
                "externalId": "8aa1a0c0-entra",
                "displayName": "Payments",
                "meta": {"resourceType": "Group"},
            },
            headers=w.auth(),
        )
        assert created.status_code == 201, created.text
        group_id = created.json()["id"]
        assert created.json()["members"] == []

        add = {
            "schemas": [PATCH_OP],
            "Operations": [
                {"op": "Add", "path": "members", "value": [{"value": dee}, {"value": eve}]}
            ],
        }
        assert c.patch(f"{S}/Groups/{group_id}", json=add, headers=w.auth()).status_code == 204
        remove = {
            "schemas": [PATCH_OP],
            "Operations": [{"op": "Remove", "path": "members", "value": [{"value": dee}]}],
        }
        assert c.patch(f"{S}/Groups/{group_id}", json=remove, headers=w.auth()).status_code == 204
        rename = {
            "schemas": [PATCH_OP],
            "Operations": [{"op": "Replace", "path": "displayName", "value": "Payments Team"}],
        }
        assert c.patch(f"{S}/Groups/{group_id}", json=rename, headers=w.auth()).status_code == 204

        lean = c.get(
            f'{S}/Groups?excludedAttributes=members&filter=displayName eq "Payments Team"',
            headers=w.auth(),
        ).json()
        assert lean["totalResults"] == 1 and "members" not in lean["Resources"][0]
        full = c.get(f"{S}/Groups/{group_id}", headers=w.auth()).json()
        assert [m["value"] for m in full["members"]] == [eve]
        assert full["externalId"] == "8aa1a0c0-entra"
        by_external = c.get(f'{S}/Groups?filter=externalId eq "8aa1a0c0-entra"', headers=w.auth())
        assert by_external.json()["totalResults"] == 1

        bad = {"Operations": [{"op": "Add", "path": "owners", "value": [{"value": dee}]}]}
        res = c.patch(f"{S}/Groups/{group_id}", json=bad, headers=w.auth())
        assert res.status_code == 400 and res.json()["scimType"] == "invalidPath"

        other = w.auth("other_admin")
        assert c.get(f"{S}/Groups/{group_id}", headers=other).status_code == 404
        assert c.get(f"{S}/Groups", headers=other).json()["totalResults"] == 0
        assert c.patch(f"{S}/Groups/{group_id}", json=add, headers=other).status_code == 404
        assert c.delete(f"{S}/Groups/{group_id}", headers=other).status_code == 404
        assert c.get(f"{S}/Users/{dee}", headers=other).status_code == 404
        theirs = c.post(
            f"{S}/Groups",
            json={"displayName": "Payments Team", "members": [{"value": eve}]},
            headers=other,
        )
        assert theirs.status_code == 201 and theirs.json()["members"] == []

        assert c.delete(f"{S}/Users/{eve}", headers=w.auth()).status_code == 204
        assert c.get(f"{S}/Groups/{group_id}", headers=w.auth()).json()["members"] == []
        assert await _flags(w) == {"dee": False}
