"""A tenant with people, keys and the fake identity provider wired into the API."""

from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from app_role_helpers import app_database_url, create_app_role, drop_app_role
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.core.config import get_settings
from checkcord.core.crypto import hash_api_key
from checkcord.core.oidc import OIDCClient
from checkcord.main import app
from checkcord.storage.postgres import create_checkcord_engine
from sim.world.idp import FakeIdP

ADMIN_URL = get_settings().database_url
ISSUER = "https://idp.test"
CLIENT_ID = "checkcord-test"
API_BASE = "https://testserver"
WEB = "https://web.test"
TABLES = (
    "scim_groups",
    "user_sessions",
    "api_keys",
    "context_event_links",
    "canonical_contexts",
    "events",
    "connection_reviews",
    "audit_log_entries",
    "enterprise_users",
)


@dataclass
class World:
    tenant_id: UUID
    other_tenant: UUID
    engine: AsyncEngine
    idp: FakeIdP
    users: dict[str, UUID] = field(default_factory=dict)
    emails: dict[str, str] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)
    key_ids: dict[str, UUID] = field(default_factory=dict)


async def admin_sql(sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    engine = create_async_engine(ADMIN_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            result = await conn.execute(text(sql), params or {})
            return list(result.fetchall()) if result.returns_rows else []
    finally:
        await engine.dispose()


# who: (tenant is "other", person is admin, key is admin)
PEOPLE = {
    "alice": (False, False, False),
    "bob": (False, False, False),
    "boss": (False, True, False),
    "gone": (False, False, False),
    "stranger": (True, True, True),
}


async def _seed(w: World) -> None:
    for who, (other, person_admin, key_admin) in PEOPLE.items():
        tenant = w.other_tenant if other else w.tenant_id
        email = f"{who.capitalize()}-{w.tenant_id.hex[:8]}@acme.test"
        user_id = uuid4()
        await admin_sql(
            "INSERT INTO enterprise_users (id, tenant_id, primary_email, display_name, active, "
            "is_admin) VALUES (:id, :t, :e, :n, :active, :admin)",
            {
                "id": user_id,
                "t": tenant,
                "e": email,
                "n": who,
                "active": who != "gone",
                "admin": person_admin,
            },
        )
        token = f"chk_test_{who}_{uuid4().hex}"
        key_id = uuid4()
        await admin_sql(
            "INSERT INTO api_keys (id, hashed_token, tenant_id, name, active, is_admin, user_id) "
            "VALUES (:id, :h, :t, :n, true, :admin, :u)",
            {
                "id": key_id,
                "h": hash_api_key(token),
                "t": tenant,
                "n": f"{who} key",
                "admin": key_admin,
                "u": user_id,
            },
        )
        w.users[who], w.emails[who], w.tokens[who], w.key_ids[who] = user_id, email, token, key_id
    admin_token = f"chk_test_admin_{uuid4().hex}"
    w.key_ids["admin"] = uuid4()
    await admin_sql(
        "INSERT INTO api_keys (id, hashed_token, tenant_id, name, active, is_admin) "
        "VALUES (:id, :h, :t, 'admin key', true, true)",
        {"id": w.key_ids["admin"], "h": hash_api_key(admin_token), "t": w.tenant_id},
    )
    w.tokens["admin"] = admin_token


@pytest_asyncio.fixture
async def world(
    monkeypatch: pytest.MonkeyPatch, async_redis_client: Redis
) -> AsyncGenerator[World, None]:
    """``async_redis_client`` empties the Redis the app writes to once the test ends."""
    tenant_id, other = uuid4(), uuid4()
    for name, value in {
        "DEFAULT_TENANT_ID": str(tenant_id),
        "OIDC_ISSUER": ISSUER,
        "OIDC_CLIENT_ID": CLIENT_ID,
        "PUBLIC_BASE_URL": API_BASE,
        "WEB_BASE_URL": WEB,
        "CORS_ALLOWED_ORIGINS": WEB,
    }.items():
        monkeypatch.setenv(name, value)
    for name in (
        "OIDC_CLIENT_SECRET",
        "OIDC_ADMIN_GROUPS",
        "OIDC_ADMIN_GROUPS_CLAIM",
        "SCIM_ADMIN_GROUPS",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    role, url = await create_app_role()
    engine = create_checkcord_engine(url)
    w = World(tenant_id, other, engine, FakeIdP(issuer=ISSUER, client_id=CLIENT_ID))
    try:
        await _seed(w)
        with app_database_url(url):
            yield w
    finally:
        await engine.dispose()
        await drop_app_role(role)
        for table in TABLES:
            await admin_sql(
                f"DELETE FROM {table} WHERE tenant_id IN (:t, :o)", {"t": tenant_id, "o": other}
            )


@contextmanager
def api(w: World) -> Iterator[TestClient]:
    with TestClient(app, base_url=API_BASE) as client:
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=w.idp.app))
        app.state.oidc = OIDCClient(get_settings(), http=http)
        yield client


def sign_in(client: TestClient, w: World, email: str, return_to: str | None = None) -> Any:
    """The callback response after the provider signs ``email`` in."""
    params = {"return_to": return_to} if return_to else None
    start = client.get("/api/v1/auth/login", params=params, follow_redirects=False)
    assert start.status_code == 302, start.text
    w.idp.email = email
    authorize = TestClient(w.idp.app).get(start.headers["location"], follow_redirects=False)
    assert authorize.status_code == 302, authorize.text
    return client.get(authorize.headers["location"], follow_redirects=False)


def signed_in(client: TestClient, w: World, who: str) -> str:
    """Sign ``who`` in on ``client``; returns the CSRF token."""
    res = sign_in(client, w, w.emails[who])
    assert res.status_code == 303, res.text
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200 and me.json()["auth"] == "session", me.text
    return str(me.json()["csrf_token"])


def bearer(w: World, who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {w.tokens[who]}"}


async def audit_rows(w: World, action: str) -> list[Any]:
    return await admin_sql(
        "SELECT channel, key_name, key_id, user_id, user_email, params, record_ids "
        "FROM audit_log_entries WHERE tenant_id = :t AND action = :a ORDER BY created_at",
        {"t": w.tenant_id, "a": action},
    )
