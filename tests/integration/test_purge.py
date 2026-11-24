"""Deleting events, sources, connections and people, and the retention jobs."""

import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app_role_helpers import app_database_url, create_app_role, drop_app_role
from fastapi.testclient import TestClient
from key_helpers import KeySpec, seeded_keys
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.cli import commands as cli
from checkcord.core.config import get_settings
from checkcord.main import app
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.models.raw_payloads import RawChatPayload
from checkcord.services import purge
from checkcord.services.audiences import AUDIENCE_TOPIC
from checkcord.storage import audit, redis_trees
from checkcord.storage.postgres import PostgresEventStore, create_checkcord_engine, tenant_context

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ADMIN_URL = get_settings().database_url
START = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
EMBEDDING = [0.1] * 1024
TABLES = (
    "context_event_links",
    "canonical_contexts",
    "events",
    "meeting_discourse_segments",
    "chat_users",
    "audience_containers",
    "user_repo_affinity",
    "notion_installations",
    "audit_log_entries",
)


@dataclass
class World:
    tenant_id: UUID
    engine: AsyncEngine
    redis: Redis
    users: dict[str, UUID]
    tokens: dict[str, str]


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
    tenant_id = uuid4()
    tokens = {who: f"purge_{who}_{uuid4().hex}" for who in ("a", "b", "admin")}
    emails = {who: f"{who}-{tenant_id}@acme.example" for who in ("a", "b")}
    specs = [
        KeySpec(tokens["a"], tenant_id, user_email=emails["a"]),
        KeySpec(tokens["b"], tenant_id, user_email=emails["b"]),
        KeySpec(tokens["admin"], tenant_id, admin=True),
    ]
    async with seeded_keys(specs) as by_email:
        users = {who: by_email[email] for who, email in emails.items()}
        await _admin(
            "INSERT INTO chat_users (tenant_id, platform, native_id, display_name, internal_id, "
            "metadata) VALUES (:t, 'slack', 'U_A', 'a', :a, '{}')",
            {"t": tenant_id, "a": users["a"]},
        )
        role, url = await create_app_role()
        engine = create_checkcord_engine(url)
        try:
            with app_database_url(url):
                yield World(tenant_id, engine, async_redis_client, users, tokens)
        finally:
            await engine.dispose()
            await drop_app_role(role)
            for table in TABLES:
                await _admin(f"DELETE FROM {table} WHERE tenant_id = :t", {"t": tenant_id})


def _access(*readers: UUID) -> AccessControl:
    if not readers:
        return AccessControl(is_public=True)
    return AccessControl(is_public=False, allowed_identity_ids=[str(r) for r in readers])


async def _event(
    w: World,
    n: int,
    *,
    container: str = "C1",
    platform: SourcePlatform = SourcePlatform.SLACK,
    readers: tuple[UUID, ...] = (),
    author: AuthorInfo | None = None,
    connection: str | None = None,
    happened_at: datetime | None = None,
    source_id: str | None = None,
    tenant_id: UUID | None = None,
) -> UUID:
    tenant_id = tenant_id or w.tenant_id
    event = CanonicalEvent(
        tenant_id=tenant_id,
        source_platform=platform,
        source_type=SourceType.MESSAGE,
        source_id=source_id or f"{container}:{n}:{uuid4().hex[:6]}",
        happened_at=happened_at or START + timedelta(minutes=n),
        author=author or AuthorInfo(source_id="someone"),
        content_raw=f"payment retries secret {n}",
        content_clean=f"payment retries secret {n}",
        access_control=_access(*readers),
        audience_container=container,
        connection=connection,
    )
    async with tenant_context(w.engine, tenant_id) as conn:
        assert await PostgresEventStore(conn).save_event(event)
    return event.event_id


async def _record(w: World, *event_ids: UUID, origin: UUID | None = None) -> UUID:
    async with tenant_context(w.engine, w.tenant_id) as conn:
        store = PostgresEventStore(conn)
        record = await store.save_canonical_context(
            w.tenant_id, "payment retries", 0.9, embedding=EMBEDDING, base_event_id=origin
        )
        for event_id in event_ids:
            await store.link_context_to_event(w.tenant_id, record, event_id)
    return record


async def _count(w: World, sql: str, **params: Any) -> int:
    [(n,)] = await _admin(sql, {"t": w.tenant_id, **params})
    return int(n)


async def _records(w: World) -> set[UUID]:
    rows = await _admin(
        "SELECT id FROM canonical_contexts WHERE tenant_id = :t", {"t": w.tenant_id}
    )
    return {r[0] for r in rows}


async def _events(w: World) -> set[UUID]:
    rows = await _admin("SELECT event_id FROM events WHERE tenant_id = :t", {"t": w.tenant_id})
    return {r[0] for r in rows}


async def _access_of(w: World, record: UUID) -> dict[str, Any]:
    [(ac,)] = await _admin(
        "SELECT access_control FROM canonical_contexts WHERE id = :r", {"r": record}
    )
    return dict(ac)


async def _purge(w: World, selection: purge.Selection, **kwargs: Any) -> purge.Purged:
    return await purge.purge(
        w.engine, w.tenant_id, selection, actor=audit.CLI_ACTOR, redis=w.redis, **kwargs
    )


async def test_purge_event_deletes_its_records_and_recomputes_shared_ones(world: World) -> None:
    w = world
    only = await _event(w, 1, readers=(w.users["a"],))
    origin = await _event(w, 2)
    kept = await _event(w, 3)
    r_only = await _record(w, only, origin=only)
    r_shared = await _record(w, origin, only, origin=origin)
    r_survivor = await _record(w, kept, only, origin=kept)
    assert (await _access_of(w, r_survivor))["is_public"] is True

    with TestClient(app) as client:
        res = client.get(
            f"/api/v1/graph/node/context:{r_only}?repo_identifier=acme/x",
            headers={"Authorization": f"Bearer {w.tokens['a']}"},
        )
        assert res.status_code == 200

    result = await _purge(w, purge.events([only]))
    assert (result.events, result.records_deleted, result.records_updated) == (1, 1, 2)
    assert await _events(w) == {origin, kept}
    assert await _records(w) == {r_shared, r_survivor}
    assert (
        await _count(w, "SELECT count(*) FROM context_event_links WHERE event_id = :e", e=only) == 0
    )
    assert (await _access_of(w, r_survivor))["is_public"] is True

    async with tenant_context(w.engine, w.tenant_id, user_id=w.users["a"]) as conn:
        found = await PostgresEventStore(conn).find_similar_contexts(
            w.tenant_id, EMBEDDING, threshold=0.5, query_text="payment retries"
        )
    assert r_only not in {UUID(str(f["id"])) for f in found}
    with TestClient(app) as client:
        res = client.get(
            f"/api/v1/graph/node/context:{r_only}?repo_identifier=acme/x",
            headers={"Authorization": f"Bearer {w.tokens['a']}"},
        )
        assert res.status_code == 404

    [(action, params, count)] = await _admin(
        "SELECT action, params, result_count FROM audit_log_entries "
        "WHERE tenant_id = :t AND action = 'data.purged'",
        {"t": w.tenant_id},
    )
    assert params["selection"] == "event" and params["records_deleted"] == 1 and count == 1
    assert "secret" not in json.dumps(params)
    [(_, fields)] = await w.redis.xrange(AUDIENCE_TOPIC)
    request = CanonicalEvent.from_redis_dict({k.decode(): v.decode() for k, v in fields.items()})
    assert set(request.metadata["canonical_context_ids"]) == {
        str(r) for r in (r_only, r_shared, r_survivor)
    }


async def test_deleting_the_base_event_promotes_the_next_one(world: World) -> None:
    w = world
    origin = await _event(w, 1)
    other = await _event(w, 2, readers=(w.users["a"],))
    record = await _record(w, origin, other, origin=origin)
    assert (await _access_of(w, record))["is_public"] is True
    await _purge(w, purge.events([origin]))
    assert record in await _records(w)
    assert await _events(w) == {other}
    [(base, summary)] = await _admin(
        "SELECT base_event_id, summary FROM canonical_contexts WHERE id = :r", {"r": record}
    )
    assert base == other and summary == "payment retries secret 2"
    access = await _access_of(w, record)
    assert access["is_public"] is False
    assert access["allowed_identity_ids"] == [str(w.users["a"])]


async def test_deleting_the_last_event_deletes_the_record(world: World) -> None:
    w = world
    origin = await _event(w, 1)
    record = await _record(w, origin, origin=origin)
    await _purge(w, purge.events([origin]))
    assert record not in await _records(w)


async def test_dry_run_counts_without_deleting(world: World) -> None:
    w = world
    e1, e2 = await _event(w, 1), await _event(w, 2)
    await _record(w, e1, origin=e1)
    await _record(w, e2, e1, origin=e2)

    found = await purge.count(w.engine, w.tenant_id, purge.events([e1]))
    assert (found.events, found.records_deleted, found.records_updated) == (1, 1, 1)
    assert await _events(w) == {e1, e2}
    assert len(await _records(w)) == 2
    assert await _count(w, "SELECT count(*) FROM audit_log_entries WHERE tenant_id = :t") == 0


async def test_purge_source_removes_its_container_and_chat_state_in_this_tenant_only(
    world: World,
) -> None:
    w = world
    other_tenant = uuid4()
    gone = [await _event(w, n) for n in range(3)]
    kept = await _event(w, 9, container="C2")
    theirs = await _event(w, 1, tenant_id=other_tenant)
    await _record(w, *gone, origin=gone[0])
    for tenant, container in ((w.tenant_id, "C1"), (w.tenant_id, "C2"), (other_tenant, "C1")):
        await _admin(
            "INSERT INTO audience_containers (tenant_id, platform, container, is_public) "
            "VALUES (:t, 'slack', :c, true)",
            {"t": tenant, "c": container},
        )
    for tenant, channel in ((w.tenant_id, "C1"), (w.tenant_id, "C2"), (other_tenant, "C1")):
        await redis_trees.push_to_channel_buffer(
            w.redis,
            RawChatPayload(
                tenant_id=tenant,
                platform=SourcePlatform.SLACK,
                channel_id=channel,
                message_id="1.0",
                author_id="U_X",
                happened_at=START,
                content_raw="hello",
                content_clean="hello",
                access_control=AccessControl(is_public=True),
            ),
        )

    try:
        found = await purge.count(w.engine, w.tenant_id, purge.source("slack", "C1"))
        assert found.events == 3
        result = await _purge(w, purge.source("slack", "C1"), batch_size=2)
        assert (result.events, result.records_deleted, result.containers) == (3, 1, 1)
        assert result.chat_state == 1
        assert await _events(w) == {kept}
        assert await _records(w) == set()
        rows = await _admin(
            "SELECT tenant_id, container FROM audience_containers WHERE tenant_id IN (:t, :o)",
            {"t": w.tenant_id, "o": other_tenant},
        )
        assert set(rows) == {(w.tenant_id, "C2"), (other_tenant, "C1")}
        buffers = set(await redis_trees.get_active_channel_buffers(w.redis))
        assert buffers == {
            (w.tenant_id, SourcePlatform.SLACK, "C2"),
            (other_tenant, SourcePlatform.SLACK, "C1"),
        }
        [(still,)] = await _admin(
            "SELECT count(*) FROM events WHERE tenant_id = :o", {"o": other_tenant}
        )
        assert still == 1 and theirs
    finally:
        for table in ("events", "audience_containers"):
            await _admin(f"DELETE FROM {table} WHERE tenant_id = :o", {"o": other_tenant})


async def test_purge_record_removes_the_events_linked_only_to_it(world: World) -> None:
    w = world
    mine, shared = await _event(w, 1), await _event(w, 2)
    record = await _record(w, mine, shared, origin=mine)
    other = await _record(w, shared, origin=shared)

    found = await purge.count(w.engine, w.tenant_id, purge.record(record))
    assert (found.events, found.records_deleted) == (1, 1)
    result = await _purge(w, purge.record(record))
    assert result.events == 1 and result.records_deleted == 1
    assert await _events(w) == {shared}
    assert await _records(w) == {other}


async def test_meeting_extractions_go_with_their_meeting(world: World) -> None:
    w = world
    meeting = await _event(w, 1, platform=SourcePlatform.ZOOM, container="M1")
    segment, adr = uuid4(), uuid4()
    await _admin(
        "INSERT INTO meeting_discourse_segments (segment_id, parent_event_id, tenant_id, "
        "text_content) VALUES (:s, :e, :t, 'we chose retries')",
        {"s": segment, "e": meeting, "t": w.tenant_id},
    )
    await _admin(
        "INSERT INTO extracted_adrs (id, segment_id, tenant_id, decision_title, context, "
        "final_decision) VALUES (:a, :s, :t, 'Retries', 'ctx', 'retry')",
        {"a": adr, "s": segment, "t": w.tenant_id},
    )
    derived = await _event(w, 2, container="M1", source_id=f"extraction-adr-{adr}")
    await _record(w, derived, origin=derived)
    unrelated = await _event(w, 3, container="M1")

    result = await _purge(w, purge.events([meeting]))
    assert (result.events, result.segments, result.records_deleted) == (2, 1, 1)
    assert await _events(w) == {unrelated}
    assert await _count(w, "SELECT count(*) FROM extracted_adrs WHERE tenant_id = :t") == 0


async def test_retention_deletes_only_old_events(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    now = datetime(2001, 3, 5, tzinfo=UTC)
    old = await _event(w, 1, happened_at=datetime(2001, 1, 1, tzinfo=UTC))
    new = await _event(w, 2, happened_at=datetime(2001, 3, 1, tzinfo=UTC))
    await _record(w, old, origin=old)

    assert await purge.purge_expired(w.engine, w.redis, now=now) == 0
    assert await _events(w) == {old, new}

    monkeypatch.setenv("EVENT_RETENTION_DAYS", "30")
    get_settings.cache_clear()
    assert await purge.purge_expired(w.engine, w.redis, now=now) == 1
    assert await _events(w) == {new}
    assert await _records(w) == set()
    [(params,)] = await _admin(
        "SELECT params FROM audit_log_entries WHERE tenant_id = :t AND action = 'data.purged'",
        {"t": w.tenant_id},
    )
    assert params["selection"] == "retention"


async def _cli(*argv: str) -> None:
    # The CLI runs its own event loop.
    await asyncio.to_thread(cli.main, list(argv))


async def test_disconnect_keeps_content_unless_purge(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    w = world
    key = "notion:WS1"
    for workspace in ("WS1", "WS2"):
        await _admin(
            "INSERT INTO notion_installations (tenant_id, notion_workspace_id, access_token) "
            "VALUES (:t, :w, 'x')",
            {"t": w.tenant_id, "w": workspace},
        )
    ingested = [
        await _event(w, n, platform=SourcePlatform.NOTION, container="p", connection=key)
        for n in range(2)
    ]
    await _record(w, *ingested, origin=ingested[0])
    other = await _event(w, 5, platform=SourcePlatform.NOTION, connection="notion:WS2")

    await _cli("disconnect", "notion", "WS2", "--tenant", str(w.tenant_id))
    out = capsys.readouterr().out
    assert "1 events" in out and "checkcord disconnect notion WS2 --purge" in out
    assert other in await _events(w)

    await _cli("disconnect", "notion", "WS1", "--purge", "--yes", "--tenant", str(w.tenant_id))
    out = capsys.readouterr().out
    assert "Disconnected notion WS1" in out and "events:" in out
    assert await _events(w) == {other}
    assert await _records(w) == set()
    assert await _count(w, "SELECT count(*) FROM notion_installations WHERE tenant_id = :t") == 0

    await _cli("disconnect", "notion", "WS2", "--purge", "--yes", "--tenant", str(w.tenant_id))
    assert await _events(w) == set()


async def test_purge_commands_dry_run_and_confirmation(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    event = await _event(w, 1)
    tenant = ("--tenant", str(w.tenant_id))

    await _cli("purge", "event", "--id", str(event), "--dry-run", *tenant)
    assert "events:" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda _: "n")
    with pytest.raises(SystemExit):
        await _cli("purge", "event", "--id", str(event), *tenant)
    assert await _events(w) == {event}

    monkeypatch.setattr("builtins.input", lambda _: "y")
    await _cli("purge", "source", "--platform", "slack", "--container", "C1", *tenant)
    assert await _events(w) == set()


async def test_users_delete_removes_the_person_everywhere(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    w = world
    a, b = w.users["a"], w.users["b"]
    by_id = await _event(w, 1, author=AuthorInfo(source_id="x", internal_id=a))
    by_native = await _event(w, 2, author=AuthorInfo(source_id="U_A"))
    seen = await _event(w, 3, readers=(a, b))
    record = await _record(w, seen, origin=seen)
    await _admin(
        "INSERT INTO audience_containers (tenant_id, platform, container, is_public, member_ids) "
        "VALUES (:t, 'slack', 'C1', false, ARRAY[CAST(:a AS uuid), CAST(:b AS uuid)])",
        {"t": w.tenant_id, "a": a, "b": b},
    )
    await _admin(
        "INSERT INTO user_repo_affinity (tenant_id, user_id, repo_identifier) "
        "VALUES (:t, :a, 'acme/x')",
        {"t": w.tenant_id, "a": a},
    )

    await _cli(
        "users", "delete", f"A-{w.tenant_id}@ACME.example", "--yes", "--tenant", str(w.tenant_id)
    )
    assert "Deleted user" in capsys.readouterr().out

    assert by_id not in await _events(w) and by_native not in await _events(w)
    [(ids, ac)] = await _admin(
        "SELECT access_allowed_ids, access_control FROM events WHERE event_id = :e", {"e": seen}
    )
    assert ids == [b] and str(a) not in json.dumps(ac)
    assert (await _access_of(w, record))["allowed_identity_ids"] == [str(b)]
    [(members,)] = await _admin(
        "SELECT member_ids FROM audience_containers WHERE tenant_id = :t", {"t": w.tenant_id}
    )
    assert members == [b]
    for table, column in (
        ("enterprise_users", "id"),
        ("api_keys", "user_id"),
        ("chat_users", "internal_id"),
        ("user_repo_affinity", "user_id"),
    ):
        assert await _count(w, f"SELECT count(*) FROM {table} WHERE {column} = :a", a=a) == 0
    [(params, record_ids)] = await _admin(
        "SELECT params, record_ids FROM audit_log_entries "
        "WHERE tenant_id = :t AND action = 'user.deleted'",
        {"t": w.tenant_id},
    )
    assert params["events"] == 2 and record_ids == [str(a)]
    assert "acme.example" not in json.dumps(params)

    with TestClient(app) as client:
        res = client.get(
            "/api/v1/graph/node/context:x?repo_identifier=acme/x",
            headers={"Authorization": f"Bearer {w.tokens['a']}"},
        )
        assert res.status_code == 401


async def test_scim_delete(world: World) -> None:
    w = world
    a = w.users["a"]
    await _event(w, 1, author=AuthorInfo(source_id="x", internal_id=a))
    ticket = await _event(
        w, 2, platform=SourcePlatform.JIRA, author=AuthorInfo(source_id="acc-b"), readers=(a,)
    )
    await _event(
        w,
        3,
        platform=SourcePlatform.JIRA,
        author=AuthorInfo(source_id="acc-a", email=f"A-{w.tenant_id}@ACME.example"),
        readers=(a,),
    )
    other_admin = f"purge_other_{uuid4().hex}"
    probe = "/api/v1/graph/node/context:x?repo_identifier=acme/x"
    async with seeded_keys([KeySpec(other_admin, uuid4(), admin=True)]):
        with TestClient(app) as client:
            auth = {"Authorization": f"Bearer {w.tokens['admin']}"}
            assert (
                client.get(probe, headers={"Authorization": f"Bearer {w.tokens['a']}"}).status_code
                == 404
            )
            foreign = client.delete(
                f"/scim/v2/Users/{a}", headers={"Authorization": f"Bearer {other_admin}"}
            )
            assert foreign.status_code == 404
            assert client.delete(f"/scim/v2/Users/{a}", headers=auth).status_code == 204
            assert client.get(f"/scim/v2/Users/{a}", headers=auth).status_code == 404
            assert client.delete(f"/scim/v2/Users/{a}", headers=auth).status_code == 404
            assert (
                client.get(probe, headers={"Authorization": f"Bearer {w.tokens['a']}"}).status_code
                == 401
            )
    assert await _events(w) == {ticket}
    [(channel,)] = await _admin(
        "SELECT channel FROM audit_log_entries WHERE tenant_id = :t AND action = 'user.deleted'",
        {"t": w.tenant_id},
    )
    assert channel == "scim"


async def test_admin_purge_endpoints(world: World) -> None:
    w = world
    e1, e2 = await _event(w, 1), await _event(w, 2, container="C2")
    record = await _record(w, e2, origin=e2)
    with TestClient(app) as client:
        auth = {"Authorization": f"Bearer {w.tokens['admin']}"}
        not_admin = {"Authorization": f"Bearer {w.tokens['b']}"}
        body = {"event_ids": [str(e1)], "dry_run": True}
        assert (
            client.post("/api/v1/admin/purge/event", json=body, headers=not_admin).status_code
            == 403
        )
        res = client.post("/api/v1/admin/purge/event", json=body, headers=auth)
        assert res.status_code == 200 and res.json()["events"] == 1
        assert await _events(w) == {e1, e2}
        res = client.post(
            "/api/v1/admin/purge/source",
            json={"platform": "slack", "container": "C1"},
            headers=auth,
        )
        assert res.json()["events"] == 1
        res = client.post(
            "/api/v1/admin/purge/record", json={"record_id": str(record)}, headers=auth
        )
        assert res.json()["records_deleted"] == 1
    assert await _events(w) == set()


async def test_only_the_owner_can_delete_audit_entries(world: World) -> None:
    w = world
    old, recent = uuid4(), uuid4()
    for entry, created in ((old, datetime(2000, 1, 1, tzinfo=UTC)), (recent, datetime.now(UTC))):
        await _admin(
            "INSERT INTO audit_log_entries (id, created_at, tenant_id, channel, action) "
            "VALUES (:i, :c, :t, 'cli', 'test.entry')",
            {"i": entry, "c": created, "t": w.tenant_id},
        )
    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_context(w.engine, w.tenant_id) as conn:
            await conn.execute(text("DELETE FROM audit_log_entries WHERE id = :i"), {"i": old})
    with pytest.raises(DBAPIError, match="permission denied"):
        await purge.prune_audit(w.engine, 3650 * 2)

    owner = create_async_engine(ADMIN_URL)
    try:
        assert await purge.prune_audit(owner, 3650 * 2, dry_run=True) >= 1
        assert await purge.prune_audit(owner, 3650 * 2) >= 1
    finally:
        await owner.dispose()
    rows = await _admin(
        "SELECT id, action FROM audit_log_entries WHERE tenant_id = :t", {"t": w.tenant_id}
    )
    assert {r[0] for r in rows if r[1] == "test.entry"} == {recent}
    assert "audit.pruned" in {r[1] for r in rows}
