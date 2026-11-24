"""Connections in review: held material is invisible until an admin releases it."""

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app_role_helpers import app_database_url, create_app_role, drop_app_role
from fastapi.testclient import TestClient
from key_helpers import KeySpec, everyone_reads, seeded_keys
from pr_check_fakes import FakeJudge
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.core.config import get_settings
from checkcord.main import app
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.pipeline.clustering import ClusteringWorker
from checkcord.pipeline.consumer import PipelineConsumer
from checkcord.pipeline.linkage import LinkageWorker
from checkcord.services import review
from checkcord.storage import audit
from checkcord.storage.postgres import (
    PostgresEventStore,
    create_checkcord_engine,
    tenant_context,
)
from checkcord.workers.context_state_compiler import ContextStateCompilerWorker
from checkcord.workers.pr_status_reporter import PRStatusWorker

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ADMIN_URL = get_settings().database_url
START = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
HELD = "jira:held.example.net"
LIVE = "jira:live.example.net"
REPO = "acme/review"
TABLES = (
    "ast_context_edges",
    "ast_nodes",
    "context_event_links",
    "canonical_contexts",
    "events",
    "connection_reviews",
    "audit_log_entries",
)


class CaptureBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    async def publish(self, topic: str, event: Any) -> str:
        self.published.append((topic, event))
        return "0-0"

    def take(self, topic: str) -> list[Any]:
        taken = [e for t, e in self.published if t == topic]
        self.published = [(t, e) for t, e in self.published if t != topic]
        return taken


class AlwaysSame:
    async def judge_linkage(self, summary: str, content: str) -> str:
        return "same"

    async def embed_text(self, text: str) -> list[float]:
        return [1.0] + [0.0] * 1023


@dataclass
class FakeGitHub:
    comments: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)

    async def get_repository(self, owner: str, repo: str) -> dict[str, Any]:
        return {"private": True, "visibility": "private", "default_branch": "main"}

    async def get_collaborators(self, owner: str, repo: str) -> list[str]:
        return []

    async def create_issue_comment(self, **kwargs: Any) -> bool:
        self.comments.append(kwargs["body"])
        return True

    async def list_issue_comments(self, owner: str, repo: str, n: int) -> list[dict[str, Any]]:
        return []

    async def update_issue_comment(self, owner: str, repo: str, cid: int, body: str) -> bool:
        return False

    async def create_commit_status(self, **kwargs: Any) -> bool:
        self.statuses.append(kwargs["state"])
        return True


@dataclass
class World:
    tenant_id: UUID
    other_tenant: UUID
    engine: AsyncEngine
    redis: Redis
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
    tenant_id, other = uuid4(), uuid4()
    tokens = {who: f"review_{who}_{uuid4().hex}" for who in ("user", "admin", "other_admin")}
    specs = [
        KeySpec(tokens["user"], tenant_id, user_email=f"u-{tenant_id}@acme.example"),
        KeySpec(tokens["admin"], tenant_id, admin=True),
        KeySpec(tokens["other_admin"], other, admin=True),
    ]
    async with seeded_keys(specs):
        await everyone_reads(tenant_id, HELD, LIVE)
        role, url = await create_app_role()
        engine = create_checkcord_engine(url)
        try:
            with app_database_url(url):
                yield World(tenant_id, other, engine, async_redis_client, tokens)
        finally:
            await engine.dispose()
            await drop_app_role(role)
            for table in TABLES:
                await _admin(
                    f"DELETE FROM {table} WHERE tenant_id IN (:t, :o)", {"t": tenant_id, "o": other}
                )


def _event(
    w: World, n: int, connection: str, *, platform: SourcePlatform = SourcePlatform.JIRA
) -> CanonicalEvent:
    return CanonicalEvent(
        tenant_id=w.tenant_id,
        source_platform=platform,
        source_type=SourceType.TICKET_CREATED,
        source_id=f"{connection}:{n}:{uuid4().hex[:6]}",
        happened_at=START + timedelta(minutes=n),
        author=AuthorInfo(source_id="someone"),
        content_raw=f"payment retries stay idempotent {n}",
        content_clean=f"payment retries stay idempotent {n}",
        access_control=AccessControl(is_public=True),
        audience_container=REPO if platform == SourcePlatform.GITHUB else None,
        connection=None if platform == SourcePlatform.GITHUB else connection,
    )


async def _ingest(w: World, *events: CanonicalEvent) -> list[UUID]:
    """Store events and run them through clustering and linkage; the records they joined."""
    bus = CaptureBus()
    model = AlwaysSame()
    consumer = PipelineConsumer(bus, w.engine, embedder=model)
    clustering = ClusteringWorker(bus=bus, engine=w.engine)
    linkage = LinkageWorker(bus=bus, engine=w.engine, llm_client=model)
    joined = []
    for event in events:
        await consumer.process_message(event)
        [pending] = bus.take("events.clustering.pending")
        await clustering.process_message(pending)
        [linking] = bus.take("events.linkage.pending")
        await linkage.process_message(linking)
        [(record,)] = await _admin(
            "SELECT canonical_id FROM context_event_links WHERE event_id = :e",
            {"e": event.event_id},
        )
        joined.append(record)
    return joined


async def _review(w: World, key: str, on: bool = True) -> None:
    await review.set_review(w.engine, w.tenant_id, key, on, audit.CLI_ACTOR)


async def _held_of(record: UUID) -> str | None:
    [(held,)] = await _admin(
        "SELECT held_connection FROM canonical_contexts WHERE id = :r", {"r": record}
    )
    return held


def _get(token: str, path: str) -> Any:
    with TestClient(app) as client:
        return client.get(path, headers={"Authorization": f"Bearer {token}"})


def _send(token: str, method: str, path: str, body: dict[str, Any]) -> Any:
    with TestClient(app) as client:
        return client.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"})


def _mcp_summaries(token: str, tool: str, args: dict[str, Any]) -> str:
    with TestClient(app) as client:
        res = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {token}",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": args},
            },
        )
    assert res.status_code == 200, res.text
    body = res.json()["result"]
    assert not body.get("isError"), body
    return str(body["content"][0]["text"])


async def _attach_to_repo(w: World, record: UUID) -> None:
    node = uuid4()
    await _admin(
        "INSERT INTO ast_nodes (id, tenant_id, repo_identifier, file_path, symbol_name, "
        "node_type, merkle_hash) VALUES (:id, :t, :repo, 'src/pay.py', :sym, "
        "'function_definition', 'h')",
        {"id": node, "t": w.tenant_id, "repo": REPO, "sym": f"pay_{node.hex[:6]}"},
    )
    await _admin(
        "INSERT INTO ast_context_edges (ast_node_id, canonical_id, tenant_id, status) "
        "VALUES (:n, :c, :t, 'active')",
        {"n": node, "c": record, "t": w.tenant_id},
    )


def _readable_by_every_path(w: World, record: UUID) -> dict[str, bool]:
    node = f"/api/v1/graph/node/context:{record}?repo_identifier={REPO}"
    return {
        "node": _get(w.tokens["user"], node).status_code == 200,
        "node_as_admin": _get(w.tokens["admin"], node).status_code == 200,
        "raw_content": _get(
            w.tokens["admin"], f"/api/v1/admin/raw-content/context:{record}?repo_identifier={REPO}"
        ).status_code
        == 200,
        "decisions": "payment retries stay idempotent"
        in _get(w.tokens["user"], f"/api/v1/graph/decisions/active?repo_identifier={REPO}").text,
        "mcp_decisions": str(record)
        in _mcp_summaries(w.tokens["user"], "view_active_decisions", {}),
        "mcp_search": str(record)
        in _mcp_summaries(
            w.tokens["user"], "search_conceptual_context", {"query": "payment retries"}
        ),
    }


async def _store_sees(w: World, record: UUID) -> dict[str, bool]:
    async with tenant_context(w.engine, w.tenant_id) as conn:
        store = PostgresEventStore(conn)
        found = await store.find_similar_contexts(
            w.tenant_id, [1.0] + [0.0] * 1023, threshold=0.5, query_text="payment retries"
        )
        direct = await store.get_canonical_context(w.tenant_id, record)
        events = await conn.scalar(
            text(
                "SELECT count(*) FROM events e JOIN context_event_links l "
                "ON l.event_id = e.event_id WHERE l.canonical_id = :r"
            ),
            {"r": record},
        )
    return {
        "similar": record in {UUID(str(f["id"])) for f in found},
        "direct": direct is not None,
        "events": bool(events),
    }


@pytest.fixture
def _search_embeds() -> Any:
    with patch("checkcord.api.mcp_http._embedder", return_value=AlwaysSame()):
        yield


async def test_held_record_is_unreadable_until_released(world: World, _search_embeds: Any) -> None:
    w = world
    await _review(w, HELD)
    [record] = await _ingest(w, _event(w, 1, HELD))
    await _attach_to_repo(w, record)
    await _admin("UPDATE canonical_contexts SET context_type = 'adr' WHERE id = :r", {"r": record})
    assert await _held_of(record) == HELD
    [(held_event,)] = await _admin(
        "SELECT held_connection FROM events WHERE tenant_id = :t", {"t": w.tenant_id}
    )
    assert held_event == HELD

    assert not any(_readable_by_every_path(w, record).values())
    assert not any((await _store_sees(w, record)).values())

    released = await review.release_connection(
        w.engine, w.tenant_id, HELD, audit.CLI_ACTOR, w.redis
    )
    assert released.counts() == {"events": 1, "records": 1}
    assert all(_readable_by_every_path(w, record).values())
    assert all((await _store_sees(w, record)).values())

    entries = await w.redis.xrange("contexts.created")
    announced = [
        CanonicalEvent.from_redis_dict({k.decode(): v.decode() for k, v in f.items()})
        for _, f in entries
    ]
    assert str(record) in {e.source_id for e in announced if e.tenant_id == w.tenant_id}
    async with tenant_context(w.engine, w.tenant_id) as conn:
        state = await review.states(conn, w.tenant_id)
    assert state[HELD] is False


async def test_held_event_never_joins_a_live_record_and_the_reverse(world: World) -> None:
    w = world
    [live] = await _ingest(w, _event(w, 1, LIVE))
    await _review(w, HELD)
    [held] = await _ingest(w, _event(w, 2, HELD))
    assert held != live
    assert await _held_of(held) == HELD and await _held_of(live) is None

    [held_again] = await _ingest(w, _event(w, 3, HELD))
    assert held_again == held
    [live_again] = await _ingest(w, _event(w, 4, LIVE))
    assert live_again == live


async def test_database_refuses_links_between_held_and_live(world: World) -> None:
    w = world
    [live] = await _ingest(w, _event(w, 1, LIVE))
    await _review(w, HELD)
    [held] = await _ingest(w, _event(w, 2, HELD))
    [(held_event,)] = await _admin(
        "SELECT event_id FROM context_event_links WHERE canonical_id = :r", {"r": held}
    )
    [(live_event,)] = await _admin(
        "SELECT event_id FROM context_event_links WHERE canonical_id = :r", {"r": live}
    )

    for record, event in ((live, held_event), (held, live_event)):
        with pytest.raises(DBAPIError, match="held and live material cannot be linked"):
            async with tenant_context(w.engine, w.tenant_id, include_held=True) as conn:
                await PostgresEventStore(conn).link_context_to_event(w.tenant_id, record, event)

    with pytest.raises(DBAPIError, match="held and live material cannot be linked"):
        async with tenant_context(w.engine, w.tenant_id, include_held=True) as conn:
            await conn.execute(
                text("UPDATE canonical_contexts SET held_connection = NULL WHERE id = :r"),
                {"r": held},
            )
    with pytest.raises(DBAPIError):
        async with tenant_context(w.engine, w.tenant_id) as conn:
            await conn.execute(
                text("UPDATE events SET held_connection = :k WHERE event_id = :e"),
                {"k": HELD, "e": live_event},
            )
    assert await _held_of(held) == HELD and await _held_of(live) is None


async def test_reject_removes_the_record_and_its_events(world: World) -> None:
    w = world
    await _review(w, HELD)
    first, second = await _ingest(w, _event(w, 1, HELD)), await _ingest(w, _event(w, 50, "x:y"))
    [(n,)] = await _admin("SELECT count(*) FROM events WHERE tenant_id = :t", {"t": w.tenant_id})
    assert n == 2

    purged = await review.reject_record(w.engine, w.tenant_id, first[0], audit.CLI_ACTOR, w.redis)
    assert purged is not None and purged.records_deleted == 1 and purged.events == 1
    assert await review.reject_record(w.engine, w.tenant_id, second[0], audit.CLI_ACTOR) is None

    await _ingest(w, _event(w, 2, HELD))
    whole = await review.reject_connection(w.engine, w.tenant_id, HELD, audit.CLI_ACTOR, w.redis)
    assert whole.records_deleted == 1 and whole.events == 1
    rows = await _admin(
        "SELECT held_connection FROM events WHERE tenant_id = :t "
        "UNION ALL SELECT held_connection FROM canonical_contexts WHERE tenant_id = :t",
        {"t": w.tenant_id},
    )
    assert rows == [(None,), (None,)]
    actions = await _admin(
        "SELECT action FROM audit_log_entries WHERE tenant_id = :t AND action = 'review.rejected'",
        {"t": w.tenant_id},
    )
    assert len(actions) == 2
    async with tenant_context(w.engine, w.tenant_id) as conn:
        assert (await review.states(conn, w.tenant_id))[HELD] is True


async def test_release_one_record(world: World) -> None:
    w = world
    await _review(w, HELD)
    [record] = await _ingest(w, _event(w, 1, HELD))
    released = await review.release_record(w.engine, w.tenant_id, record, audit.CLI_ACTOR)
    assert released is not None and released.counts() == {"events": 1, "records": 1}
    assert await _held_of(record) is None
    assert await review.release_record(w.engine, w.tenant_id, record, audit.CLI_ACTOR) is None
    [new] = await _ingest(w, _event(w, 2, HELD))
    assert new != record and await _held_of(new) == HELD


async def test_github_writers_skip_held_records(world: World, monkeypatch: Any) -> None:
    w = world
    await _review(w, f"github:{REPO}")
    [held] = await _ingest(w, _event(w, 1, "", platform=SourcePlatform.GITHUB))
    assert await _held_of(held) == f"github:{REPO}"
    await _admin("UPDATE canonical_contexts SET severity = 'hard_block' WHERE id = :r", {"r": held})
    await _attach_to_repo(w, held)

    github = FakeGitHub()
    status = await _report(w, github, held)
    assert status == "success" and not github.comments

    compiler = ContextStateCompilerWorker(w.redis, w.engine)
    compiler.pr_service = AsyncMock()
    compiler.pr_service.propose_context_update.return_value = True
    with (
        patch("checkcord.workers.context_state_compiler.GitHubRestClient", return_value=github),
        patch(
            "checkcord.workers.context_state_compiler.github_token_for",
            AsyncMock(return_value="token"),
        ),
        patch("checkcord.workers.context_state_compiler.asyncio.sleep", AsyncMock()),
    ):
        await compiler._debounced_compile(w.tenant_id, REPO)
    for call in compiler.pr_service.propose_context_update.call_args_list:
        assert str(held) not in json.dumps(call.kwargs, default=str)
        assert "payment retries" not in call.kwargs.get("architecture_md", "")

    await review.release_connection(w.engine, w.tenant_id, f"github:{REPO}", audit.CLI_ACTOR)
    assert await _report(w, FakeGitHub(), held) == "failure"


async def _report(w: World, github: FakeGitHub, record: UUID) -> str:
    payload = {
        "repository_full_name": REPO,
        "pull_request_number": 7,
        "head_sha": uuid4().hex,
        "tenant_id": str(w.tenant_id),
        "affected_contexts": [
            {
                "symbol_name": "pay",
                "file_path": "src/pay.py",
                "node_type": "function_definition",
                "change_type": "modified",
                "canonical_ids": [str(record)],
            }
        ],
    }
    with (
        patch("checkcord.workers.pr_status_reporter.github_token_for", AsyncMock(return_value="t")),
        patch("checkcord.workers.pr_status_reporter.GitHubRestClient", return_value=github),
    ):
        await PRStatusWorker(w.redis, w.engine, FakeJudge())._process_message(
            "ci.pr.context_checked", "g", "1-0", {"payload": json.dumps(payload)}
        )
    return github.statuses[-1]


async def test_admin_review_read_sees_held_but_normal_reads_do_not(world: World) -> None:
    w = world
    await _review(w, HELD)
    [record] = await _ingest(w, _event(w, 1, HELD))

    res = _get(w.tokens["admin"], "/api/v1/admin/review")
    assert res.status_code == 200
    [held] = res.json()["items"]
    assert held["id"] == str(record) and held["connection"] == HELD
    assert held["event_count"] == 1 and held["sources"][0]["platform"] == "jira"
    filtered = _get(w.tokens["admin"], "/api/v1/admin/review?connection=other:x").json()
    assert filtered["items"] == []

    assert _get(w.tokens["user"], "/api/v1/admin/review").status_code == 403
    node = f"/api/v1/graph/node/context:{record}?repo_identifier={REPO}"
    assert _get(w.tokens["admin"], node).status_code == 404
    [(n,)] = await _admin(
        "SELECT count(*) FROM audit_log_entries WHERE tenant_id = :t AND action = 'review.list'",
        {"t": w.tenant_id},
    )
    assert n == 2

    res = _send(
        w.tokens["admin"], "POST", "/api/v1/admin/review/release", {"record_id": str(record)}
    )
    assert res.status_code == 200 and res.json()["records"] == 1
    assert _get(w.tokens["admin"], node).status_code == 200
    bad = _send(w.tokens["admin"], "POST", "/api/v1/admin/review/release", {})
    assert bad.status_code == 422


async def test_new_connections_start_in_review_when_the_setting_is_on(
    world: World, monkeypatch: Any
) -> None:
    w = world
    monkeypatch.setattr(get_settings(), "review_new_connections", True)
    [first] = await _ingest(w, _event(w, 1, "jira:new.example.net"))
    assert await _held_of(first) == "jira:new.example.net"

    async with tenant_context(w.engine, w.tenant_id) as conn:
        await review.mark_new(conn, w.tenant_id, LIVE, review=False)
    async with tenant_context(w.engine, w.tenant_id) as conn:
        assert (await review.states(conn, w.tenant_id))[LIVE] is True

    monkeypatch.setattr(get_settings(), "review_new_connections", False)
    [second] = await _ingest(w, _event(w, 2, "jira:other.example.net"))
    assert await _held_of(second) is None
    [third] = await _ingest(w, _event(w, 3, "jira:new.example.net"))
    assert await _held_of(third) == "jira:new.example.net"


async def test_severity_change_fails_the_check_and_is_audited(world: World) -> None:
    w = world
    [record] = await _ingest(w, _event(w, 1, LIVE))
    await _attach_to_repo(w, record)
    assert await _report(w, FakeGitHub(), record) == "success"

    path = f"/api/v1/admin/records/{record}/severity"
    assert _send(w.tokens["user"], "PATCH", path, {"severity": "blocking"}).status_code == 403
    assert _send(w.tokens["admin"], "PATCH", path, {"severity": "high"}).status_code == 422
    res = _send(w.tokens["admin"], "PATCH", path, {"severity": "blocking"})
    assert res.status_code == 200 and res.json() == {"id": str(record), "severity": "blocking"}
    assert await _report(w, FakeGitHub(), record) == "failure"

    [(params, ids)] = await _admin(
        "SELECT params, record_ids FROM audit_log_entries "
        "WHERE tenant_id = :t AND action = 'context.severity_changed'",
        {"t": w.tenant_id},
    )
    assert params == {"from": "advisory", "to": "blocking"} and ids == [str(record)]

    await _review(w, HELD)
    [held] = await _ingest(w, _event(w, 2, HELD))
    held_path = f"/api/v1/admin/records/{held}/severity"
    assert _send(w.tokens["admin"], "PATCH", held_path, {"severity": "blocking"}).status_code == 404


async def test_type_change_is_audited_and_kept(world: World) -> None:
    w = world
    [record] = await _ingest(w, _event(w, 1, LIVE))
    assert str(record) not in _mcp_summaries(w.tokens["user"], "view_active_decisions", {})

    path = f"/api/v1/admin/records/{record}/type"
    assert _send(w.tokens["user"], "PATCH", path, {"type": "adr"}).status_code == 403
    assert _send(w.tokens["admin"], "PATCH", path, {"type": "decision"}).status_code == 422
    res = _send(w.tokens["admin"], "PATCH", path, {"type": "adr"})
    assert res.status_code == 200 and res.json() == {"id": str(record), "type": "adr"}

    [(params, ids)] = await _admin(
        "SELECT params, record_ids FROM audit_log_entries "
        "WHERE tenant_id = :t AND action = 'record.type_changed'",
        {"t": w.tenant_id},
    )
    assert params == {"from": "general", "to": "adr"} and ids == [str(record)]

    assert await _ingest(w, _event(w, 2, LIVE)) == [record]
    [(kind,)] = await _admin(
        "SELECT context_type FROM canonical_contexts WHERE id = :r", {"r": record}
    )
    assert kind == "adr"
    assert str(record) in _mcp_summaries(w.tokens["user"], "view_active_decisions", {})


async def test_one_tenant_cannot_review_another(world: World) -> None:
    w = world
    await _review(w, HELD)
    [record] = await _ingest(w, _event(w, 1, HELD))
    other = w.tokens["other_admin"]

    assert _get(other, "/api/v1/admin/review").json()["items"] == []
    res = _send(other, "POST", "/api/v1/admin/review/release", {"connection": HELD})
    assert res.status_code == 200 and res.json()["records"] == 0
    res = _send(other, "POST", "/api/v1/admin/review/release", {"record_id": str(record)})
    assert res.status_code == 404
    res = _send(other, "POST", "/api/v1/admin/review/reject", {"record_id": str(record)})
    assert res.status_code == 404
    res = _send(other, "POST", "/api/v1/admin/review/reject", {"connection": HELD})
    assert res.status_code == 200 and res.json()["records_deleted"] == 0
    assert await _held_of(record) == HELD
    async with tenant_context(w.engine, w.other_tenant) as conn:
        assert await review.held_counts(conn, w.other_tenant) == {}


async def test_release_and_reject_a_selection(world: World) -> None:
    w = world
    admin = w.tokens["admin"]
    await _review(w, HELD)
    [held] = await _ingest(w, _event(w, 1, HELD))
    [live] = await _ingest(w, _event(w, 50, "x:y"))
    unknown = str(uuid4())

    [row] = [
        c
        for c in _get(admin, "/api/v1/admin/connections").json()["items"]
        if c["connection"] == HELD
    ]
    assert row["state"] == "review" and row["events"] == 1 and row["held_records"] == 1

    body = {"record_ids": [str(held), str(live), unknown]}
    res = _send(admin, "POST", "/api/v1/admin/review/release", body)
    assert res.status_code == 200
    assert res.json() == {
        "events": 1,
        "records": 1,
        "record_ids": [str(held)],
        "not_held": [str(live), unknown],
    }
    assert await _held_of(held) is None

    [again] = await _ingest(w, _event(w, 2, HELD))
    res = _send(admin, "POST", "/api/v1/admin/review/reject", {"record_ids": [str(again)]})
    assert res.status_code == 200
    assert res.json()["records_deleted"] == 1 and res.json()["not_held"] == []

    both = {"record_id": str(held), "record_ids": [str(held)]}
    assert _send(admin, "POST", "/api/v1/admin/review/reject", both).status_code == 422
    empty = {"record_ids": []}
    assert _send(admin, "POST", "/api/v1/admin/review/release", empty).status_code == 422
