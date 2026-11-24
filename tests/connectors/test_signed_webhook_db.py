import json
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app_role_helpers import app_database_url, create_app_role, drop_app_role
from fastapi.testclient import TestClient
from key_helpers import KeySpec, seeded_keys
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.connectors import installations
from checkcord.connectors.signed_webhook.routes import TOPIC, signature
from checkcord.core.config import get_settings
from checkcord.main import app
from checkcord.models.event import CanonicalEvent
from checkcord.services import connections, scope
from checkcord.storage import audit
from checkcord.storage.postgres import create_checkcord_engine

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@dataclass
class Hook:
    tenant_id: UUID
    id: UUID
    secret: str
    reader: UUID
    email: str
    engine: AsyncEngine
    redis: Redis


async def _admin(sql: str, params: dict[str, Any]) -> None:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def hook(async_redis_client: Redis) -> AsyncGenerator[Hook, None]:
    tenant_id = uuid4()
    email = f"reader-{tenant_id.hex[:8]}@example.com"
    secret = f"sig_{uuid4().hex}"
    async with seeded_keys([KeySpec(f"k_{uuid4().hex}", tenant_id, user_email=email)]) as users:
        role, url = await create_app_role()
        engine = create_checkcord_engine(url)
        try:
            saved = await installations.save(
                engine, tenant_id, "webhook", "status-page", secret=secret
            )
            with app_database_url(url):
                yield Hook(
                    tenant_id,
                    saved.id,
                    secret,
                    users[email],
                    email,
                    engine,
                    async_redis_client,
                )
        finally:
            await engine.dispose()
            await drop_app_role(role)
            for table in ("connector_installations", "connection_reviews", "audit_log_entries"):
                await _admin(f"DELETE FROM {table} WHERE tenant_id = :t", {"t": tenant_id})
            await _admin("DELETE FROM connection_scopes WHERE tenant_id = :t", {"t": tenant_id})
            scope._cache.clear()


def _post(
    client: TestClient, hook: Hook, body: dict[str, Any], *, secret: str | None = None, age: int = 0
) -> Any:
    raw = json.dumps(body).encode()
    ts = str(int(time.time()) - age)
    return client.post(
        f"/api/v1/webhooks/signed/{hook.id}",
        content=raw,
        headers={
            "X-Checkcord-Timestamp": ts,
            "X-Checkcord-Signature": signature(secret or hook.secret, ts, raw),
            "Content-Type": "application/json",
        },
    )


async def _published(redis: Redis) -> list[CanonicalEvent]:
    found = await redis.xrange(TOPIC)
    return [
        CanonicalEvent.from_redis_dict({k.decode(): v.decode() for k, v in fields.items()})
        for _, fields in found
    ]


async def test_signed_event_is_published_with_its_audience(hook: Hook) -> None:
    body = {
        "id": "inc-1",
        "text": "Checkout is failing for card payments",
        "container": "payments",
        "audience": {"emails": [hook.email.upper(), "nobody@example.com"]},
    }
    with TestClient(app) as client:
        response = _post(client, hook, body)

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "accepted"
    assert response.json()["unknown_emails"] == 1
    [event] = await _published(hook.redis)
    assert event.tenant_id == hook.tenant_id
    assert event.connection == "webhook:status-page"
    assert event.access_control.allowed_identity_ids == [hook.reader]
    assert event.access_control.is_public is False


async def test_bad_signature_stale_timestamp_and_unknown_id_are_refused(hook: Hook) -> None:
    body = {"id": "inc-2", "text": "x"}
    with TestClient(app) as client:
        wrong = _post(client, hook, body, secret="not-the-secret")
        stale = _post(client, hook, body, age=600)
        unsigned = client.post(f"/api/v1/webhooks/signed/{hook.id}", json=body)
        unknown = client.post(f"/api/v1/webhooks/signed/{uuid4()}", json=body)

    assert [r.status_code for r in (wrong, stale, unsigned, unknown)] == [401] * 4
    assert await _published(hook.redis) == []


async def test_scope_keeps_excluded_containers_out(hook: Hook) -> None:
    await scope.change(
        hook.engine, hook.tenant_id, "webhook:status-page", audit.CLI_ACTOR, exclude=["secret-ops"]
    )
    scope._cache.clear()
    with TestClient(app) as client:
        skipped = _post(client, hook, {"id": "a", "text": "x", "container": "SECRET-OPS"})
        kept = _post(client, hook, {"id": "b", "text": "y", "container": "payments"})

    assert skipped.json()["status"] == "skipped"
    assert kept.json()["status"] == "accepted"
    assert [e.source_id for e in await _published(hook.redis)] == ["b"]


async def test_connection_is_listed_rotated_and_disconnected(hook: Hook) -> None:
    listed = await connections.list_connections(hook.engine, hook.tenant_id)
    assert [(c.provider, c.identifier) for c in listed] == [("webhook", "status-page")]

    again = await installations.save(
        hook.engine, hook.tenant_id, "webhook", "status-page", secret="new", replace_secret=False
    )
    assert again == installations.Saved(hook.id, False)
    kept = await installations.by_id(hook.engine, "webhook", hook.id)
    assert kept is not None and kept.secret == hook.secret
    assert await installations.by_id(hook.engine, "datadog", hook.id) is None

    assert await connections.disconnect(hook.engine, hook.tenant_id, "webhook", "status-page")
    assert await installations.by_id(hook.engine, "webhook", hook.id) is None


async def test_other_tenants_cannot_see_the_connection(hook: Hook) -> None:
    other = uuid4()
    assert await connections.list_connections(hook.engine, other) == []
    assert not await connections.disconnect(hook.engine, other, "webhook", "status-page")
