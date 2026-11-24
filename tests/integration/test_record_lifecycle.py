"""Retired records are hidden from every serving path; suggestions change nothing until accepted."""

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
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.core.config import get_settings
from checkcord.main import app
from checkcord.models.common import AccessControl, AuthorInfo, CodeMetadata
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.pipeline.clustering import ClusteringWorker
from checkcord.pipeline.consumer import PipelineConsumer
from checkcord.pipeline.linkage import LinkageWorker
from checkcord.services import purge, records
from checkcord.storage import audit
from checkcord.storage.postgres import create_checkcord_engine, tenant_context
from checkcord.workers.context_state_compiler import ContextStateCompilerWorker
from checkcord.workers.pr_status_reporter import PRStatusWorker

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ADMIN_URL = get_settings().database_url
START = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
LIVE = "jira:lifecycle.example.net"
REPO = "acme/lifecycle"
TABLES = (
    "record_replacement_suggestions",
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


@dataclass
class Model:
    verdict: str = "same"
    judged: list[str] = field(default_factory=list)

    async def judge_linkage(self, summary: str, content: str) -> str:
        self.judged.append(content)
        return self.verdict

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
    engine: AsyncEngine
    redis: Redis
    tokens: dict[str, str]


async def _admin(sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    engine = create_async_engine(ADMIN_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(text("SET LOCAL app.include_retired = 'true'"))
            result = await conn.execute(text(sql), params or {})
            return list(result.fetchall()) if result.returns_rows else []
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(name="w")
async def world(async_redis_client: Redis) -> AsyncGenerator[World, None]:
    tenant_id = uuid4()
    tokens = {who: f"lifecycle_{who}_{uuid4().hex}" for who in ("user", "admin")}
    specs = [
        KeySpec(tokens["user"], tenant_id, user_email=f"u-{tenant_id}@acme.example"),
        KeySpec(tokens["admin"], tenant_id, admin=True),
    ]
    async with seeded_keys(specs):
        await everyone_reads(tenant_id, LIVE)
        role, url = await create_app_role()
        engine = create_checkcord_engine(url)
        try:
            with app_database_url(url):
                yield World(tenant_id, engine, async_redis_client, tokens)
        finally:
            await engine.dispose()
            await drop_app_role(role)
            for table in TABLES:
                await _admin(f"DELETE FROM {table} WHERE tenant_id = :t", {"t": tenant_id})


def _event(w: World, n: int, content: str, repo: str | None = None) -> CanonicalEvent:
    return CanonicalEvent(
        tenant_id=w.tenant_id,
        source_platform=SourcePlatform.JIRA,
        source_type=SourceType.TICKET_CREATED,
        source_id=f"lifecycle:{n}:{uuid4().hex[:6]}",
        happened_at=START + timedelta(days=n),
        author=AuthorInfo(source_id="someone"),
        content_raw=content,
        content_clean=content,
        access_control=AccessControl(is_public=True),
        connection=LIVE,
        code_metadata=CodeMetadata(repository=repo, branch="main") if repo else None,
        metadata={"context_type": "adr"},
    )


async def _ingest(w: World, model: Model, *events: CanonicalEvent) -> list[UUID]:
    bus = CaptureBus()
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


def _call(token: str, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    with TestClient(app) as client:
        return client.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"})


def _mcp(token: str, tool: str, args: dict[str, Any]) -> str:
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


def _served(w: World, record: UUID, summary: str = "payment retries") -> dict[str, bool]:
    node = f"/api/v1/graph/node/context:{record}?repo_identifier={REPO}"
    decisions = f"/api/v1/graph/decisions/active?repo_identifier={REPO}"
    return {
        "node": _call(w.tokens["user"], "GET", node).status_code == 200,
        "decisions": summary in _call(w.tokens["user"], "GET", decisions).text,
        "mcp_decisions": str(record) in _mcp(w.tokens["user"], "view_active_decisions", {}),
        "mcp_search": str(record)
        in _mcp(w.tokens["user"], "search_conceptual_context", {"query": "payment retries"}),
    }


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


async def _check(w: World, record: UUID) -> str:
    github = FakeGitHub()
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


async def _context_file(w: World) -> str:
    compiler = ContextStateCompilerWorker(w.redis, w.engine)
    compiler.pr_service = AsyncMock()
    compiler.pr_service.propose_context_update.return_value = True
    with (
        patch(
            "checkcord.workers.context_state_compiler.GitHubRestClient", return_value=FakeGitHub()
        ),
        patch(
            "checkcord.workers.context_state_compiler.github_token_for",
            AsyncMock(return_value="token"),
        ),
        patch("checkcord.workers.context_state_compiler.asyncio.sleep", AsyncMock()),
    ):
        await compiler._debounced_compile(w.tenant_id, REPO)
    return json.dumps(
        [c.kwargs for c in compiler.pr_service.propose_context_update.call_args_list], default=str
    )


async def _actions(w: World, action: str) -> list[Any]:
    return await _admin(
        "SELECT params, record_ids FROM audit_log_entries WHERE tenant_id = :t AND action = :a",
        {"t": w.tenant_id, "a": action},
    )


@pytest.fixture(autouse=True)
def _search_embeds() -> Any:
    with patch("checkcord.api.mcp_http._embedder", return_value=Model()):
        yield


async def test_retired_record_is_served_nowhere_but_admins_see_and_restore_it(w: World) -> None:
    [record] = await _ingest(w, Model(), _event(w, 1, "payment retries stay idempotent"))
    await _admin(
        "UPDATE canonical_contexts SET severity = 'hard_block' WHERE id = :r", {"r": record}
    )
    await _attach_to_repo(w, record)
    assert all(_served(w, record).values())
    assert await _check(w, record) == "failure"
    assert "payment retries" in await _context_file(w)

    path = f"/api/v1/admin/records/{record}"
    assert _call(w.tokens["user"], "POST", f"{path}/retire", {}).status_code == 403
    res = _call(w.tokens["admin"], "POST", f"{path}/retire", {"reason": "no longer true"})
    assert res.status_code == 200, res.text
    assert _call(w.tokens["admin"], "POST", f"{path}/retire", {}).status_code == 409

    assert not any(_served(w, record).values())
    assert await _check(w, record) == "success"
    assert "payment retries" not in await _context_file(w)

    shown = _call(w.tokens["admin"], "GET", path).json()
    assert shown["state"] == "retired" and shown["retired_reason"] == "no longer true"
    assert shown["retired_by"].startswith("key:")
    listed = _call(w.tokens["admin"], "GET", "/api/v1/admin/records?state=retired").json()
    assert [r["id"] for r in listed["items"]] == [str(record)]
    assert _call(w.tokens["admin"], "GET", "/api/v1/admin/records").json()["items"] == []
    [(params, ids)] = await _actions(w, "context.retired")
    assert params == {} and ids == [str(record)]

    assert _call(w.tokens["admin"], "POST", f"{path}/restore").status_code == 200
    assert _call(w.tokens["admin"], "POST", f"{path}/restore").status_code == 409
    assert all(_served(w, record).values())
    assert await _check(w, record) == "failure"
    assert len(await _actions(w, "context.restored")) == 1


async def test_new_events_never_join_a_retired_record(w: World) -> None:
    [old] = await _ingest(w, Model(), _event(w, 1, "payment retries stay idempotent"))
    assert await records.retire(w.engine, w.tenant_id, old, audit.CLI_ACTOR) is None
    [new] = await _ingest(w, Model(), _event(w, 2, "payment retries stay idempotent, again"))
    assert new != old


async def test_retire_needs_a_live_replacement(w: World) -> None:
    first, second = await _ingest(
        w, Model("different"), _event(w, 1, "payment retries A"), _event(w, 2, "payment retries B")
    )
    retire = records.retire
    assert await retire(w.engine, w.tenant_id, uuid4(), audit.CLI_ACTOR) == "not_found"
    assert await retire(w.engine, w.tenant_id, first, audit.CLI_ACTOR, replaced_by=first) == "self"
    assert (
        await retire(w.engine, w.tenant_id, first, audit.CLI_ACTOR, replaced_by=uuid4())
        == "replacement_not_found"
    )
    assert await retire(w.engine, w.tenant_id, first, audit.CLI_ACTOR, replaced_by=second) is None
    assert (
        await retire(w.engine, w.tenant_id, second, audit.CLI_ACTOR, replaced_by=first)
        == "replacement_not_found"
    )
    [(replaced_by,)] = await _admin(
        "SELECT replaced_by FROM canonical_contexts WHERE id = :r", {"r": first}
    )
    assert replaced_by == second


async def test_stated_replacement_is_suggested_and_changes_nothing_until_accepted(
    w: World,
) -> None:
    model = Model()
    old, new = await _ingest(
        w,
        model,
        _event(w, 1, "Decision: payment retries run in a nightly batch."),
        _event(w, 9, "Decision: payment retries run live. This supersedes the nightly batch."),
    )
    assert old != new and model.judged == []
    listed = _call(w.tokens["admin"], "GET", "/api/v1/admin/records/suggestions").json()
    [suggestion] = listed["items"]
    assert suggestion["record_id"] == str(old) and suggestion["replacement_id"] == str(new)
    assert suggestion["basis"] == "stated"
    assert _served(w, old)["mcp_decisions"] and _served(w, new)["mcp_decisions"]
    shown = _call(w.tokens["admin"], "GET", f"/api/v1/admin/records/{old}").json()
    assert shown["state"] == "live"
    assert shown["suggested_replacements"][0]["replacement_id"] == str(new)

    sid = suggestion["id"]
    other = uuid4()
    assert (await records.suggestions(w.engine, other, audit.CLI_ACTOR))[0] == []
    assert (
        await records.decide_suggestion(w.engine, other, UUID(sid), True, audit.CLI_ACTOR)
        == "not_found"
    )
    async with tenant_context(w.engine, other, include_retired=True) as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM record_replacement_suggestions"))
        ).scalar_one() == 0
    assert (
        _call(w.tokens["user"], "POST", f"/api/v1/admin/records/suggestions/{sid}/accept")
    ).status_code == 403
    res = _call(w.tokens["admin"], "POST", f"/api/v1/admin/records/suggestions/{sid}/accept")
    assert res.status_code == 200, res.text
    assert not _served(w, old)["mcp_decisions"] and _served(w, new)["mcp_decisions"]
    shown = _call(w.tokens["admin"], "GET", f"/api/v1/admin/records/{old}").json()
    assert shown["state"] == "retired" and shown["replaced_by"] == str(new)
    assert _call(w.tokens["admin"], "GET", "/api/v1/admin/records/suggestions").json() == {
        "items": [],
        "next_cursor": None,
    }
    [(params, ids)] = await _actions(w, "context.retired")
    assert params == {"replaced_by": str(new), "suggestion": sid, "basis": "stated"}
    assert ids == [str(old), str(new)]
    gone = _call(w.tokens["admin"], "POST", f"/api/v1/admin/records/suggestions/{sid}/accept")
    assert gone.status_code == 404


async def test_model_suggestion_comes_from_a_changes_verdict_and_dismiss_keeps_both(
    w: World,
) -> None:
    [old] = await _ingest(w, Model(), _event(w, 1, "payment retries go through the queue"))
    [kept] = await _ingest(w, Model("different"), _event(w, 3, "payment retries log attempts"))
    model = Model("changes")
    [new] = await _ingest(w, model, _event(w, 4, "payment retries call the processor directly"))
    assert len(model.judged) == 1 and new not in {old, kept}
    [(record_id, replacement_id, basis)] = await _admin(
        "SELECT record_id, replacement_id, basis FROM record_replacement_suggestions "
        "WHERE tenant_id = :t",
        {"t": w.tenant_id},
    )
    assert replacement_id == new and basis == "model"
    assert record_id in {old, kept}

    [suggestion] = (await records.suggestions(w.engine, w.tenant_id, audit.CLI_ACTOR))[0]
    assert (
        await records.decide_suggestion(
            w.engine, w.tenant_id, suggestion["id"], False, audit.CLI_ACTOR
        )
        is None
    )
    assert (await records.suggestions(w.engine, w.tenant_id, audit.CLI_ACTOR))[0] == []
    assert _served(w, record_id)["mcp_decisions"] and _served(w, new)["mcp_decisions"]
    assert len(await _actions(w, "context.replacement_dismissed")) == 1


async def test_no_suggestion_across_scopes(w: World) -> None:
    await _ingest(w, Model(), _event(w, 1, "payment retries run in a nightly batch"))
    model = Model("changes")
    await _ingest(
        w, model, _event(w, 2, "This supersedes the nightly batch: retries run live", repo=REPO)
    )
    assert model.judged == []
    assert (
        await _admin(
            "SELECT 1 FROM record_replacement_suggestions WHERE tenant_id = :t", {"t": w.tenant_id}
        )
        == []
    )


async def test_purge_reaches_retired_records(w: World) -> None:
    [record] = await _ingest(w, Model(), _event(w, 1, "payment retries stay idempotent"))
    assert await records.retire(w.engine, w.tenant_id, record, audit.CLI_ACTOR) is None
    provider, identifier = LIVE.split(":", 1)
    purged = await purge.purge(
        w.engine, w.tenant_id, purge.connection(provider, identifier), actor=audit.CLI_ACTOR
    )
    assert purged.records_deleted == 1
    assert (
        await _admin("SELECT 1 FROM canonical_contexts WHERE tenant_id = :t", {"t": w.tenant_id})
        == []
    )
