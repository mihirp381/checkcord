"""Stored channel and repository audiences follow the provider, and records follow them."""

import hashlib
import hmac
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app_role_helpers import app_database_url, create_app_role, drop_app_role
from fastapi.testclient import TestClient
from key_helpers import KeySpec, seeded_keys
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.adapters.chat.providers.slack.audience import (
    read_slack_channel,
    resolve_slack_audience,
)
from checkcord.core.config import get_settings
from checkcord.main import app
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.services import audiences
from checkcord.storage import audiences as stored
from checkcord.storage.audiences import Audience
from checkcord.storage.postgres import (
    PostgresEventStore,
    aggregate_access_control,
    create_checkcord_engine,
    tenant_context,
)
from checkcord.workers.context_state_compiler import ContextStateCompilerWorker

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

REPO = "acme/audiences"
START = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
ADMIN_URL = get_settings().database_url
PLATFORMS = {"slack": SourcePlatform.SLACK, "github": SourcePlatform.GITHUB}
NATIVE = {"slack": {"a": "U_A", "b": "U_B"}, "github": {"a": "alice", "b": "bob"}}


class SlackClient:
    def __init__(self, is_private: bool, members: list[str]) -> None:
        self.is_private = is_private
        self.members = members
        self.calls = 0

    async def conversations_info(self, channel: str) -> dict[str, Any]:
        self.calls += 1
        return {"ok": True, "channel": {"is_private": self.is_private}}

    async def conversations_members(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {"ok": True, "members": self.members}


@dataclass
class World:
    tenant_id: UUID
    engine: AsyncEngine
    redis: Redis
    users: dict[str, UUID]


async def _admin(sql: str, params: dict[str, Any]) -> list[Any]:
    engine = create_async_engine(ADMIN_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            result = await conn.execute(text(sql), params)
            return list(result.fetchall()) if result.returns_rows else []
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def world(async_redis_client: Redis) -> AsyncGenerator[World, None]:
    tenant_id = uuid4()
    specs = [
        KeySpec("audience_a", tenant_id, user_email=f"a-{tenant_id}@acme.example"),
        KeySpec("audience_b", tenant_id, user_email=f"b-{tenant_id}@acme.example"),
        KeySpec("audience_unbound", tenant_id),
        KeySpec("audience_admin", tenant_id, admin=True),
    ]
    async with seeded_keys(specs) as by_email:
        users = {
            "a": by_email[f"a-{tenant_id}@acme.example"],
            "b": by_email[f"b-{tenant_id}@acme.example"],
        }
        for platform, names in (("slack", NATIVE["slack"]), ("github", NATIVE["github"])):
            for who, native_id in names.items():
                await _admin(
                    "INSERT INTO chat_users (tenant_id, platform, native_id, display_name, "
                    "internal_id, metadata) VALUES (:t, :p, :n, :n, :i, '{}')",
                    {"t": tenant_id, "p": platform, "n": native_id, "i": users[who]},
                )
        role, url = await create_app_role()
        engine = create_checkcord_engine(url)
        try:
            with app_database_url(url):
                yield World(tenant_id, engine, async_redis_client, users)
        finally:
            await engine.dispose()
            await drop_app_role(role)
            for table in (
                "context_event_links",
                "context_repo_scopes",
                "canonical_contexts",
                "events",
                "chat_users",
                "audience_containers",
                "github_installations",
                "audit_log_entries",
            ):
                await _admin(f"DELETE FROM {table} WHERE tenant_id = :t", {"t": tenant_id})


def _reading(is_public: bool, *who: str, platform: str = "slack") -> audiences.Reader:
    async def read() -> audiences.Reading:
        return audiences.Reading(is_public, tuple(NATIVE[platform].get(w, w) for w in who))

    return read


async def _failing() -> audiences.Reading:
    raise audiences.ReadFailed("provider down")


async def _refresh(
    w: World, platform: str, container: str, reader: audiences.Reader
) -> audiences.Outcome:
    return await audiences.refresh(
        w.engine, w.tenant_id, platform, container, trigger="test", reader=reader
    )


async def _event(w: World, platform: str, container: str, n: int) -> UUID:
    """An event in ``container`` built with the container's current audience."""
    ac = await audiences.current(
        w.engine, w.tenant_id, platform, container, reader=_failing, is_public_hint=None
    )
    event = CanonicalEvent(
        tenant_id=w.tenant_id,
        source_platform=PLATFORMS[platform],
        source_type=SourceType.MESSAGE if platform == "slack" else SourceType.PR_OPENED,
        source_id=f"{platform}:{container}:{n}",
        happened_at=START + timedelta(minutes=n),
        author=AuthorInfo(source_id="someone"),
        content_raw=f"payment retries {container} {n}",
        content_clean=f"payment retries {container} {n}",
        access_control=ac,
        audience_container=container,
    )
    async with tenant_context(w.engine, w.tenant_id) as conn:
        assert await PostgresEventStore(conn).save_event(event)
    return event.event_id


async def _record(w: World, *event_ids: UUID) -> UUID:
    async with tenant_context(w.engine, w.tenant_id) as conn:
        store = PostgresEventStore(conn)
        record = await store.save_canonical_context(w.tenant_id, "payment retries", 0.9)
        for event_id in event_ids:
            await store.link_context_to_event(w.tenant_id, record, event_id)
    return record


def _readers(record: UUID) -> set[str]:
    readers = set()
    with TestClient(app) as client:
        for who in ("a", "b", "unbound"):
            res = client.get(
                f"/api/v1/graph/node/context:{record}?repo_identifier={REPO}",
                headers={"Authorization": f"Bearer audience_{who}"},
            )
            assert res.status_code in (200, 404)
            if res.status_code == 200:
                readers.add(who)
    return readers


EVERYONE = {"a", "b", "unbound"}


async def test_removed_member_loses_access_and_added_member_gains_it(world: World) -> None:
    await _refresh(world, "slack", "G1", _reading(False, "a", "U_GHOST"))
    record = await _record(world, await _event(world, "slack", "G1", 1))
    assert _readers(record) == {"a"}

    outcome = await audiences.refresh(
        world.engine,
        world.tenant_id,
        "slack",
        "G1",
        trigger="test",
        reader=_reading(False, "b"),
        redis=world.redis,
    )
    assert outcome.applied.events == 1
    assert outcome.applied.narrowed_records == [record]
    assert _readers(record) == {"b"}

    [(_, fields)] = await world.redis.xrange(audiences.AUDIENCE_TOPIC)
    request = CanonicalEvent.from_redis_dict({k.decode(): v.decode() for k, v in fields.items()})
    assert request.metadata["canonical_context_ids"] == [str(record)]
    compiler = ContextStateCompilerWorker(world.redis, world.engine)
    with patch.object(
        PostgresEventStore, "get_active_repos_for_compilation", AsyncMock(return_value=[REPO])
    ):
        await compiler.process_message(request)
    assert (world.tenant_id, REPO) in compiler.debounce_tasks
    compiler.debounce_tasks[(world.tenant_id, REPO)].cancel()

    first, (params,) = await _admin(
        "SELECT params FROM audit_log_entries "
        "WHERE tenant_id = :t AND action = 'audience.refreshed' ORDER BY created_at",
        {"t": world.tenant_id},
    )
    assert first[0]["members_added"] == 1 and first[0]["records_narrowed"] == 0
    assert params["trigger"] == "test"
    assert params["members_added"] == params["members_removed"] == 1
    assert params["records_narrowed"] == 1
    flat = json.dumps(params)
    assert str(world.users["a"]) not in flat and str(world.users["b"]) not in flat


async def test_repository_turning_private_restricts_its_records(world: World) -> None:
    await _refresh(world, "github", REPO, _reading(True, platform="github"))
    record = await _record(world, await _event(world, "github", REPO, 1))
    assert _readers(record) == EVERYONE

    await _refresh(world, "github", REPO, _reading(False, "a", "mallory", platform="github"))
    assert _readers(record) == {"a"}
    async with tenant_context(world.engine, world.tenant_id) as conn:
        row = await stored.get(conn, world.tenant_id, "github", REPO)
    assert row is not None and row["is_public"] is False
    assert row["member_ids"] == [world.users["a"]]


async def test_record_follows_its_base_event_not_later_ones(world: World) -> None:
    await _refresh(world, "slack", "G_WIDE", _reading(False, "a", "b"))
    await _refresh(world, "github", REPO, _reading(True, platform="github"))
    record = await _record(
        world, await _event(world, "slack", "G_WIDE", 1), await _event(world, "github", REPO, 2)
    )
    assert _readers(record) == {"a", "b"}

    await _refresh(world, "github", REPO, _reading(False, "b", platform="github"))
    assert _readers(record) == {"a", "b"}
    await _refresh(world, "slack", "G_WIDE", _reading(True))
    assert _readers(record) == EVERYONE
    await _refresh(world, "slack", "G_WIDE", _reading(False, "b"))
    assert _readers(record) == {"b"}


async def test_failed_read_keeps_audience_within_grace_then_empties(world: World) -> None:
    await _refresh(world, "slack", "G_DOWN", _reading(False, "a"))
    record = await _record(world, await _event(world, "slack", "G_DOWN", 1))

    outcome = await _refresh(world, "slack", "G_DOWN", _failing)
    assert outcome.applied.kept and outcome.error
    assert _readers(record) == {"a"}

    await _admin(
        "UPDATE audience_containers SET refreshed_at = now() - interval '25 hours' "
        "WHERE tenant_id = :t",
        {"t": world.tenant_id},
    )
    # No reader passed: the tenant has no Slack connection stored.
    outcome = await audiences.refresh(
        world.engine, world.tenant_id, "slack", "G_DOWN", trigger="test"
    )
    assert not outcome.applied.kept
    assert outcome.error and "connection removed" in outcome.error
    assert _readers(record) == set()
    async with tenant_context(world.engine, world.tenant_id) as conn:
        row = await stored.get(conn, world.tenant_id, "slack", "G_DOWN")
    assert row is not None and row["last_error"] and row["member_ids"] == []

    await _refresh(world, "slack", "G_DOWN", _reading(False, "a"))
    assert _readers(record) == {"a"}


async def test_batches_narrow_before_they_widen(world: World) -> None:
    await _refresh(world, "slack", "G_BIG", _reading(False, "a"))
    events = [await _event(world, "slack", "G_BIG", n) for n in range(7)]
    records = [await _record(world, e) for e in events] + [
        await _record(world, events[0], events[-1])
    ]
    a, b = str(world.users["a"]), str(world.users["b"])
    seen: list[set[str]] = []
    original = stored.batch

    async def checked(conn: Any, *args: Any, **kwargs: Any) -> int:
        done = await original(conn, *args, **kwargs)
        rows = (
            await conn.execute(
                text(
                    "SELECT access_control, access_allowed_ids FROM events "
                    "WHERE tenant_id = :t AND audience_container = 'G_BIG'"
                ),
                {"t": world.tenant_id},
            )
        ).all()
        grants = {
            who for _, ids in rows for who, uid in (("a", a), ("b", b)) if uid in map(str, ids)
        }
        seen.append(grants)
        assert not {"a", "b"} <= grants, "a removed member and an added one can both read"
        for record in records:
            linked = (
                await conn.execute(
                    text(
                        "SELECT e.access_control, e.access_allowed_ids FROM events e "
                        "JOIN context_event_links l ON l.event_id = e.event_id "
                        "WHERE l.canonical_id = :r"
                    ),
                    {"r": record},
                )
            ).all()
            expected = aggregate_access_control(
                [{"access_control": ac, "access_allowed_ids": ids} for ac, ids in linked]
            )
            [(actual,)] = (
                await conn.execute(
                    text("SELECT access_control FROM canonical_contexts WHERE id = :r"),
                    {"r": record},
                )
            ).all()
            assert sorted(actual["allowed_identity_ids"]) == sorted(
                expected["allowed_identity_ids"]
            )
            assert actual["is_public"] == expected["is_public"]
        return done

    with patch.object(stored, "batch", checked):
        applied = await stored.apply(
            world.engine,
            world.tenant_id,
            "slack",
            "G_BIG",
            Audience(False, frozenset({b})),
            now=datetime.now(UTC),
            grace=timedelta(hours=24),
            batch_size=2,
        )

    assert applied.events == 14 and not applied.superseded
    assert seen == [{"a"}] * 3 + [set()] + [{"b"}] * 4
    assert all(_readers(r) == {"b"} for r in records)


async def test_github_member_webhook_refreshes_repository(world: World) -> None:
    secret = "whsec-" + uuid4().hex
    await _admin(
        "INSERT INTO github_installations (id, tenant_id, repo_identifier, "
        "personal_access_token, webhook_secret, created_at, updated_at) "
        "VALUES (gen_random_uuid(), :t, :r, :p, :s, now(), now())",
        {"t": world.tenant_id, "r": REPO, "p": "pat-" + uuid4().hex, "s": secret},
    )
    await _refresh(world, "github", REPO, _reading(True, platform="github"))
    record = await _record(world, await _event(world, "github", REPO, 1))
    assert _readers(record) == EVERYONE

    body = json.dumps({"action": "added", "repository": {"full_name": REPO}}).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    rest = "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient"
    with (
        patch(f"{rest}.get_repository", AsyncMock(return_value={"private": True})),
        patch(f"{rest}.get_collaborators", AsyncMock(return_value=["bob"])),
        TestClient(app) as client,
    ):
        res = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={"X-GitHub-Event": "member", "X-Hub-Signature-256": signature},
        )
    assert res.status_code == 200 and res.json() == {"status": "audience_refreshed"}
    assert _readers(record) == {"b"}


async def test_new_events_reuse_a_fresh_stored_audience(world: World) -> None:
    first = SlackClient(True, ["U_A"])
    audience = await resolve_slack_audience(
        client=first, redis=None, db_engine=world.engine, tenant_id=world.tenant_id, channel_id="C9"
    )
    assert audience.allowed_identity_ids == [world.users["a"]]
    assert first.calls == 2

    again = SlackClient(False, [])
    audience = await resolve_slack_audience(
        client=again, redis=None, db_engine=world.engine, tenant_id=world.tenant_id, channel_id="C9"
    )
    assert again.calls == 0
    assert audience.allowed_identity_ids == [world.users["a"]]

    # An event built before a refresh is stored with the audience its container has now.
    stale = AccessControl(is_public=True)
    event = CanonicalEvent(
        tenant_id=world.tenant_id,
        source_platform=SourcePlatform.SLACK,
        source_type=SourceType.MESSAGE,
        source_id="slack:C9:late",
        happened_at=START,
        author=AuthorInfo(source_id="someone"),
        content_raw="late event",
        content_clean="late event",
        access_control=stale,
        audience_container="C9",
    )
    async with tenant_context(world.engine, world.tenant_id) as conn:
        assert await PostgresEventStore(conn).save_event(event)
    record = await _record(world, event.event_id)
    assert _readers(record) == {"a"}


async def test_scheduled_refresh_reads_due_containers(world: World) -> None:
    client = SlackClient(True, ["U_A"])
    await audiences.refresh(
        world.engine,
        world.tenant_id,
        "slack",
        "G_SCHED",
        trigger="test",
        reader=lambda: read_slack_channel(client, "G_SCHED"),
    )
    await _admin(
        "UPDATE audience_containers SET attempted_at = now() - interval '2 hours' "
        "WHERE tenant_id = :t",
        {"t": world.tenant_id},
    )
    with patch.object(audiences, "reader_for", AsyncMock(return_value=_reading(False, "b"))):
        assert await audiences.refresh_due(world.engine) >= 1
    async with tenant_context(world.engine, world.tenant_id) as conn:
        row = await stored.get(conn, world.tenant_id, "slack", "G_SCHED")
    assert row is not None and row["member_ids"] == [world.users["b"]]


def _api(method: str, path: str, who: str = "admin", body: Any = None) -> Any:
    with TestClient(app) as client:
        return client.request(
            method,
            f"/api/v1/admin{path}",
            json=body,
            headers={"Authorization": f"Bearer audience_{who}"},
        )


async def _stored(world: World, container: str) -> dict[str, Any]:
    async with tenant_context(world.engine, world.tenant_id) as conn:
        row = await stored.get(conn, world.tenant_id, "slack", container)
    assert row is not None
    return row


async def test_an_unmatched_member_is_counted_and_a_link_lets_them_read(world: World) -> None:
    from checkcord.services import accounts, doctor
    from checkcord.storage import audit

    await _refresh(world, "slack", "G2", _reading(False, "a", "U_NEW"))
    row = await _stored(world, "G2")
    assert (row["unmatched_count"], row["unmatched_accounts"]) == (1, ["U_NEW"])
    record = await _record(world, await _event(world, "slack", "G2", 1))
    assert _readers(record) == {"a"}
    check = await doctor.unmatched_accounts_check(world.engine, world.tenant_id)
    assert check is not None and check.warn and check.detail.startswith("1 account(s)")
    [shown] = _api("GET", "/audiences").json()["items"]
    assert (shown["unmatched_count"], shown["member_count"]) == (1, 1)

    read_again = AsyncMock(return_value=_reading(False, "a", "U_NEW"))
    with patch("checkcord.services.audiences.reader_for", read_again):
        done = await accounts.link(
            world.engine,
            world.tenant_id,
            "slack",
            "U_NEW",
            actor=audit.CLI_ACTOR,
            email=f"B-{world.tenant_id}@ACME.example",
        )
        assert done.user_id == world.users["b"]
        assert _readers(record) == {"a", "b"}
        assert (await _stored(world, "G2"))["unmatched_count"] == 0
        assert await doctor.unmatched_accounts_check(world.engine, world.tenant_id) is None

        await _admin(
            "UPDATE chat_users SET email = :e WHERE tenant_id = :t AND native_id = 'U_NEW'",
            {"e": f"b-{world.tenant_id}@acme.example", "t": world.tenant_id},
        )
        assert await accounts.unlink(
            world.engine, world.tenant_id, "slack", "U_NEW", actor=audit.CLI_ACTOR
        )
        async with tenant_context(world.engine, world.tenant_id) as conn:
            assert await accounts.link_by_email(conn, world.tenant_id, "slack") == []
        assert _readers(record) == {"a"}

    actions = await _admin(
        "SELECT action, params FROM audit_log_entries WHERE tenant_id = :t "
        "AND action LIKE 'account.%' ORDER BY created_at",
        {"t": world.tenant_id},
    )
    assert [a for a, _ in actions] == ["account.linked", "account.unlinked"]
    assert actions[0][1]["person_email"] == f"b-{world.tenant_id}@acme.example"
    triggers = await _admin(
        "SELECT params->>'trigger' FROM audit_log_entries WHERE tenant_id = :t "
        "AND action = 'audience.refreshed' ORDER BY created_at",
        {"t": world.tenant_id},
    )
    assert [t for (t,) in triggers] == ["test", "account_linked", "account_unlinked"]


async def test_admin_api_lists_links_and_unlinks_accounts(world: World) -> None:
    await _refresh(world, "slack", "G3", _reading(False, "a", "U_NEW"))
    assert _api("GET", "/accounts", who="a").status_code == 403
    body = {"platform": "slack", "account": "U_NEW", "email": f"a-{world.tenant_id}@acme.example"}
    assert _api("POST", "/accounts/link", "a", body).status_code == 403

    unmatched = _api("GET", "/accounts?matched=false").json()["items"]
    assert [(r["account"], r["unmatched_in"]) for r in unmatched] == [("U_NEW", 1)]
    page = _api("GET", "/accounts?platform=slack&limit=1").json()
    assert len(page["items"]) == 1 and page["next_cursor"]
    rest = _api("GET", f"/accounts?platform=slack&cursor={page['next_cursor']}").json()["items"]
    assert page["items"][0]["account"] not in {r["account"] for r in rest}

    missing = {**body, "email": "nobody@acme.example"}
    assert _api("POST", "/accounts/link", body=missing).status_code == 404
    with patch("checkcord.services.audiences.reader_for", AsyncMock(side_effect=RuntimeError)):
        linked = _api("POST", "/accounts/link", body=body)
        assert linked.status_code == 200 and linked.json()["user_id"] == str(world.users["a"])
        [row] = [
            r
            for r in _api("GET", "/accounts?platform=slack").json()["items"]
            if r["account"] == "U_NEW"
        ]
        assert row["link_source"] == "manual" and row["linked_by"]
        gone = {"platform": "slack", "account": "U_NEW"}
        assert _api("POST", "/accounts/unlink", body=gone).status_code == 200
        unknown = {"platform": "slack", "account": "U_NEVER"}
        assert _api("POST", "/accounts/unlink", body=unknown).status_code == 404


async def test_import_links_counts_what_it_could_not_match(world: World) -> None:
    from checkcord.services import accounts
    from checkcord.storage import audit

    rows = accounts.read_csv(
        [
            "platform,account,email",
            f"slack,U_A,A-{world.tenant_id}@acme.example",
            f"github,new-login,b-{world.tenant_id}@acme.example",
            "slack,U_Z,nobody@acme.example",
            "fax,1,x@acme.example",
        ]
    )
    result = await accounts.import_links(world.engine, world.tenant_id, rows, actor=audit.CLI_ACTOR)
    assert (result.linked, result.unknown_accounts, result.unknown_people) == (2, 1, 1)
    assert len(result.rejected) == 1
    [(internal, source)] = await _admin(
        "SELECT internal_id, link_source FROM chat_users WHERE tenant_id = :t "
        "AND platform = 'github' AND native_id = 'new-login'",
        {"t": world.tenant_id},
    )
    assert (internal, source) == (world.users["b"], "manual")


async def test_deleting_a_person_removes_their_accounts_and_what_they_wrote(world: World) -> None:
    from checkcord.services import purge
    from checkcord.storage import audit

    b_email = f"b-{world.tenant_id}@acme.example"
    authors = [
        ("slack", AuthorInfo(source_id="U_B", email="b.personal@elsewhere.example")),
        ("github", AuthorInfo(source_id="bob")),
        ("slack", AuthorInfo(source_id="U_OTHER", email=b_email.upper())),
    ]
    mine = []
    for n, (platform, author) in enumerate(authors):
        mine.append(await _authored(world, platform, author, n))
    kept = await _authored(world, "github", AuthorInfo(source_id="U_B"), 9)

    deleted = await purge.delete_person(
        world.engine, world.tenant_id, actor=audit.CLI_ACTOR, email=b_email
    )
    assert deleted is not None and deleted.purged.events == 3
    left = {
        r[0]
        for r in await _admin(
            "SELECT event_id FROM events WHERE tenant_id = :t", {"t": world.tenant_id}
        )
    }
    assert left == {kept}
    assert (
        await _admin(
            "SELECT native_id FROM chat_users WHERE tenant_id = :t AND internal_id = :u",
            {"t": world.tenant_id, "u": world.users["b"]},
        )
        == []
    )
    [(params,)] = await _admin(
        "SELECT params FROM audit_log_entries WHERE tenant_id = :t AND action = 'user.deleted'",
        {"t": world.tenant_id},
    )
    assert (params["accounts"], params["events"]) == (2, 3)


async def _authored(world: World, platform: str, author: AuthorInfo, n: int) -> UUID:
    event = CanonicalEvent(
        tenant_id=world.tenant_id,
        source_platform=PLATFORMS[platform],
        source_type=SourceType.MESSAGE,
        source_id=f"authored:{n}:{uuid4().hex[:6]}",
        happened_at=START + timedelta(minutes=n),
        author=author,
        content_raw=f"note {n}",
        content_clean=f"note {n}",
        access_control=AccessControl(is_public=True),
    )
    async with tenant_context(world.engine, world.tenant_id) as conn:
        assert await PostgresEventStore(conn).save_event(event)
    return event.event_id


async def test_unseen_accounts_are_read_at_most_fifty_at_a_time(world: World) -> None:
    from checkcord.services import accounts

    asked: list[list[str]] = []

    async def lookup(ids: list[str]) -> list[accounts.Profile]:
        asked.append(ids)
        return [
            accounts.Profile(n, n, f"B-{world.tenant_id}@acme.example" if n == "U_00" else None)
            for n in ids
        ]

    ids = [f"U_{n:02}" for n in range(60)]
    matched, unmatched = await accounts.match(world.engine, world.tenant_id, "slack", ids, lookup)
    assert [len(a) for a in asked] == [accounts.LOOKUP_LIMIT]
    assert matched == {"U_00": world.users["b"]} and len(unmatched) == 59
    [(source,)] = await _admin(
        "SELECT link_source FROM chat_users WHERE tenant_id = :t AND native_id = 'U_00'",
        {"t": world.tenant_id},
    )
    assert source == "email"

    await accounts.match(world.engine, world.tenant_id, "slack", ids, lookup)
    assert [len(a) for a in asked] == [accounts.LOOKUP_LIMIT, 10]


async def test_github_profile_email_links_a_login_and_is_read_once_a_day(world: World) -> None:
    from checkcord.services import accounts

    asked: list[list[str]] = []

    async def lookup(ids: list[str]) -> list[accounts.Profile]:
        asked.append(ids)
        return [
            accounts.Profile("carol-gh", "Carol", f"a-{world.tenant_id}@ACME.example"),
            accounts.Profile("dan-gh", "Dan", None),
        ]

    logins = ["carol-gh", "dan-gh"]
    matched, unmatched = await accounts.match(
        world.engine, world.tenant_id, "github", logins, lookup
    )
    assert matched == {"carol-gh": world.users["a"]} and unmatched == ["dan-gh"]
    [(source,)] = await _admin(
        "SELECT link_source FROM chat_users WHERE tenant_id = :t AND native_id = 'carol-gh'",
        {"t": world.tenant_id},
    )
    assert source == "profile"
    await accounts.match(world.engine, world.tenant_id, "github", logins, lookup)
    assert asked == [logins]
