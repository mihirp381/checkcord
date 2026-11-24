"""What an admin removes is not stored again when it is delivered again."""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
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

from checkcord.cli import commands as cli
from checkcord.core.config import get_settings
from checkcord.main import app
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.models.raw_payloads import RawChatPayload
from checkcord.services import chat_messages, purge, review
from checkcord.storage import audit
from checkcord.storage.postgres import (
    PostgresEventStore,
    create_checkcord_engine,
    source_id_hash,
    tenant_context,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ADMIN_URL = get_settings().database_url
START = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
HELD = "jira:held.example.net"
TABLES = (
    "context_event_links",
    "canonical_contexts",
    "events",
    "removed_sources",
    "connection_reviews",
    "chat_users",
    "notion_installations",
    "audit_log_entries",
)


@dataclass
class World:
    tenant_id: UUID
    other: UUID
    engine: AsyncEngine
    redis: Redis
    email: str
    admin_token: str


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
    email = f"p-{tenant_id}@acme.example"
    token = f"removed_admin_{uuid4().hex}"
    specs = [KeySpec(f"removed_p_{uuid4().hex}", tenant_id, user_email=email)]
    specs.append(KeySpec(token, tenant_id, admin=True))
    async with seeded_keys(specs):
        role, url = await create_app_role()
        engine = create_checkcord_engine(url)
        try:
            with app_database_url(url):
                yield World(tenant_id, other, engine, async_redis_client, email, token)
        finally:
            await engine.dispose()
            await drop_app_role(role)
            for table in TABLES:
                await _admin(
                    f"DELETE FROM {table} WHERE tenant_id IN (:t, :o)", {"t": tenant_id, "o": other}
                )


def _event(
    tenant_id: UUID,
    source_id: str,
    *,
    platform: SourcePlatform = SourcePlatform.JIRA,
    connection: str | None = None,
    email: str | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        tenant_id=tenant_id,
        source_platform=platform,
        source_type=SourceType.TICKET_CREATED,
        source_id=source_id,
        happened_at=START,
        author=AuthorInfo(source_id="someone", email=email),
        content_raw="payment retries stay idempotent",
        content_clean="payment retries stay idempotent",
        access_control=AccessControl(is_public=True),
        connection=connection,
    )


async def _deliver(w: World, event: CanonicalEvent) -> bool:
    async with tenant_context(w.engine, event.tenant_id, include_held=True) as conn:
        return await PostgresEventStore(conn).save_event(event)


async def _stored(tenant_id: UUID, source_id: str) -> int:
    [(n,)] = await _admin(
        "SELECT count(*) FROM events WHERE tenant_id = :t AND source_id = :s",
        {"t": tenant_id, "s": source_id},
    )
    return int(n)


async def _cli(*argv: str) -> None:
    await asyncio.to_thread(cli.main, list(argv))


async def test_rejected_record_is_not_stored_again(world: World) -> None:
    w = world
    await review.set_review(w.engine, w.tenant_id, HELD, True, audit.CLI_ACTOR)
    sid = "jira:held.example.net:10002:created"
    event = _event(w.tenant_id, sid, connection=HELD)
    assert await _deliver(w, event)
    async with tenant_context(w.engine, w.tenant_id, include_held=True) as conn:
        store = PostgresEventStore(conn)
        record = await store.save_canonical_context(
            w.tenant_id, "payment retries", 0.9, base_event_id=event.event_id, held_connection=HELD
        )
        await store.link_context_to_event(w.tenant_id, record, event.event_id)

    rejected = await review.reject_record(w.engine, w.tenant_id, record, audit.CLI_ACTOR, w.redis)
    assert rejected is not None and rejected.events == 1
    [(platform, digest, reason)] = await _admin(
        "SELECT source_platform, source_id_hash, reason FROM removed_sources WHERE tenant_id = :t",
        {"t": w.tenant_id},
    )
    assert (platform, digest, reason) == ("jira", source_id_hash(sid), "record")

    assert not await _deliver(w, _event(w.tenant_id, sid, connection=HELD))
    assert await _stored(w.tenant_id, sid) == 0
    update = "jira:held.example.net:10002:updated:1"
    assert await _deliver(w, _event(w.tenant_id, update, connection=HELD))
    [(held,)] = await _admin(
        "SELECT held_connection FROM events WHERE tenant_id = :t AND source_id = :s",
        {"t": w.tenant_id, "s": update},
    )
    assert held == HELD


async def test_purged_event_stays_out_and_another_tenant_is_unaffected(world: World) -> None:
    w = world
    sid = "jira:acme:1:created"
    event = _event(w.tenant_id, sid)
    assert await _deliver(w, event)
    await purge.purge(w.engine, w.tenant_id, purge.events([event.event_id]), actor=audit.CLI_ACTOR)
    assert not await _deliver(w, _event(w.tenant_id, sid))
    assert await _stored(w.tenant_id, sid) == 0
    assert await _deliver(w, _event(w.other, sid))
    assert await _stored(w.other, sid) == 1


async def test_deleted_persons_messages_stay_out(world: World) -> None:
    w = world
    payload = RawChatPayload(
        tenant_id=w.tenant_id,
        platform=SourcePlatform.SLACK,
        channel_id="C1",
        message_id="1.0",
        author_id="U_P",
        author_email=w.email,
        happened_at=START,
        content_raw="we keep retries idempotent",
        content_clean="we keep retries idempotent",
        access_control=AccessControl(is_public=True),
    )
    assert await chat_messages.store(w.engine, payload)
    deleted = await purge.delete_person(
        w.engine, w.tenant_id, actor=audit.CLI_ACTOR, email=w.email, redis=w.redis
    )
    assert deleted is not None and deleted.purged.events == 1
    assert not await chat_messages.store(w.engine, payload)
    assert await _stored(w.tenant_id, chat_messages.source_id("C1", "1.0")) == 0


async def test_disconnect_with_purge_remembers_nothing(world: World) -> None:
    w = world
    key, sid = "notion:WS1", "notion-page-1"
    install = (
        "INSERT INTO notion_installations (tenant_id, notion_workspace_id, access_token) "
        "VALUES (:t, 'WS1', 'x')"
    )
    await _admin(install, {"t": w.tenant_id})
    assert await _deliver(
        w, _event(w.tenant_id, sid, platform=SourcePlatform.NOTION, connection=key)
    )
    await _cli("disconnect", "notion", "WS1", "--purge", "--yes", "--tenant", str(w.tenant_id))
    assert await _stored(w.tenant_id, sid) == 0
    await _admin(install, {"t": w.tenant_id})
    assert await _deliver(
        w, _event(w.tenant_id, sid, platform=SourcePlatform.NOTION, connection=key)
    )
    assert await _stored(w.tenant_id, sid) == 1


async def test_removed_clear_lets_it_back(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    w = world
    sids = ["jira:acme:7:created", "jira:acme:8:created"]
    events = [_event(w.tenant_id, s) for s in sids]
    for event in events:
        assert await _deliver(w, event)
    await purge.purge(
        w.engine, w.tenant_id, purge.events([e.event_id for e in events]), actor=audit.CLI_ACTOR
    )

    await _cli("removed", "count", "--tenant", str(w.tenant_id))
    assert "jira:" in capsys.readouterr().out
    await _cli("stats", "--tenant", str(w.tenant_id))
    assert "Removed items remembered: 2" in capsys.readouterr().out

    with TestClient(app) as client:
        auth = {"Authorization": f"Bearer {w.admin_token}"}
        res = client.get("/api/v1/admin/purge/removed", headers=auth)
        assert res.status_code == 200 and res.json() == {"platforms": {"jira": 2}, "total": 2}
        res = client.post(
            "/api/v1/admin/purge/removed/clear", json={"platform": "slack"}, headers=auth
        )
        assert res.json() == {"cleared": 0}

    assert not await _deliver(w, _event(w.tenant_id, sids[0]))
    await _cli("removed", "clear", "--platform", "jira", "--yes", "--tenant", str(w.tenant_id))
    assert "Forgot 2" in capsys.readouterr().out
    assert await _deliver(w, _event(w.tenant_id, sids[0]))
    assert await _stored(w.tenant_id, sids[0]) == 1
    counts = await _admin(
        "SELECT result_count FROM audit_log_entries "
        "WHERE tenant_id = :t AND action = 'removed.cleared' ORDER BY created_at",
        {"t": w.tenant_id},
    )
    assert [c for (c,) in counts] == [0, 2]
