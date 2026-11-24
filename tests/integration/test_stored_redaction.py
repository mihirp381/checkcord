"""Stored redaction on every read path, the sweep and recompute, and the usage and stats views."""

import json
import secrets
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app_role_helpers import app_database_url, create_app_role, drop_app_role
from fastapi.testclient import TestClient
from key_helpers import KeySpec, delete_contexts, seeded_keys
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pr_check_fakes import FakeJudge, FakePullRequest
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.core.config import get_settings
from checkcord.core.redaction import StoredRedaction
from checkcord.llm import usage
from checkcord.main import app
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import AccessVisibility, AclType, SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.models.pr_checks import PRContextCheckedPayload
from checkcord.pipeline import redaction as write_side
from checkcord.pipeline.redaction import RedactedEntity, RedactionOutput
from checkcord.proxy.context import DynamicContextCache
from checkcord.services.context_formatter import ContextFormatterService
from checkcord.services.repo_audience import RepoAudience
from checkcord.storage import audit
from checkcord.storage.models import (
    ASTContextEdge,
    ASTNode,
    AuditLogEntry,
    CanonicalContext,
    ContextEventLink,
    ModelUsage,
)
from checkcord.storage.postgres import (
    PostgresEventStore,
    app_bypass_rls_context,
    create_checkcord_engine,
    events_table,
    tenant_context,
)
from checkcord.workers.pr_status_reporter import PRStatusWorker

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

REPO = "acme/redaction"
FILE = "src/billing.py"
SYMBOL = "chargeCard"
VECTOR = [1.0] + [0.0] * 1023
EMAIL = "carol@redact.test"
PHONE = "+1 (555) 123-4567"
TOKEN = "ghp_" + secrets.token_hex(18)
MODEL_ONLY = "hunter2-secret"
RAW = f"Charge retries: mail {EMAIL}, call {PHONE}, key {TOKEN}, password {MODEL_ONLY}."
LEGACY = f"Legacy charge rule: mail {EMAIL}, call {PHONE}, key {TOKEN}."
PUBLIC = {"is_public": True, "visibility": "public", "acl_type": "custom"}
ADMIN_URL = get_settings().database_url


def _model(entities: list[RedactedEntity]) -> MagicMock:
    structured = AsyncMock()
    structured.ainvoke.return_value = RedactionOutput(entities=entities)
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


def _down() -> MagicMock:
    llm = MagicMock()
    llm.with_structured_output.side_effect = RuntimeError("provider down")
    return llm


FINDS_PASSWORD = [RedactedEntity(text=MODEL_ONLY, entity_type="TOKEN")]


def _leaks(body: str) -> list[str]:
    return [s for s in (EMAIL, PHONE, TOKEN, MODEL_ONLY) if s in body]


@dataclass(frozen=True)
class World:
    tenant: UUID
    reader: str
    admin: str
    plain: str
    stored: UUID
    legacy: UUID
    engine: AsyncEngine


@pytest.fixture
def model_calls() -> Iterator[list[str]]:
    """Every chat model call is recorded and refused."""
    calls: list[str] = []

    def _refuse(name: str) -> Any:
        async def _async(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError("a read path called a chat model")

        def _sync(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError("a read path called a chat model")

        return _async if name.startswith("a") else _sync

    with (
        patch.object(BaseChatModel, "agenerate", _refuse("agenerate")),
        patch.object(BaseChatModel, "generate", _refuse("generate")),
        patch.object(BaseChatModel, "astream", _refuse("astream")),
        patch.object(BaseChatModel, "stream", _refuse("stream")),
    ):
        yield calls


class _Embedder:
    async def embed_text(self, text: str) -> list[float]:
        return VECTOR


@pytest.fixture(autouse=True)
def _fake_embedder() -> Iterator[None]:
    with patch("checkcord.api.mcp_http._embedder", return_value=_Embedder()):
        yield


async def _seed(tenant: UUID) -> tuple[UUID, UUID]:
    engine = create_async_engine(ADMIN_URL)
    node, event, chat_event = uuid4(), uuid4(), uuid4()
    stored_copy = await write_side.redact_for_storage(RAW, llm_client=_model(FINDS_PASSWORD))
    assert MODEL_ONLY not in stored_copy.text
    try:
        async with tenant_context(engine, tenant) as conn:
            store = PostgresEventStore(conn)
            stored = await store.save_canonical_context(
                tenant_id=tenant,
                summary=RAW,
                confidence_score=9.0,
                embedding=VECTOR,
                access_control=PUBLIC,
                context_type="adr",
                redaction=stored_copy,
            )
            legacy = await store.save_canonical_context(
                tenant_id=tenant,
                summary=LEGACY,
                confidence_score=8.0,
                embedding=VECTOR,
                access_control=PUBLIC,
                context_type="constraint",
            )
            await store.save_event(
                CanonicalEvent(
                    event_id=event,
                    tenant_id=tenant,
                    source_platform=SourcePlatform.SLACK,
                    source_type=SourceType.MESSAGE,
                    source_id=f"msg-{event}",
                    happened_at=datetime.now(UTC),
                    author=AuthorInfo(source_id="U1", display_name="Someone"),
                    content_raw=LEGACY,
                    content_clean=LEGACY,
                    access_control=AccessControl(
                        visibility=AccessVisibility.PUBLIC, acl_type=AclType.CUSTOM, is_public=True
                    ),
                    connection="slack:T-redact",
                )
            )
            await store.save_event(
                CanonicalEvent(
                    event_id=chat_event,
                    tenant_id=tenant,
                    source_platform=SourcePlatform.SLACK,
                    source_type=SourceType.MESSAGE,
                    source_id=f"msg-{chat_event}",
                    happened_at=datetime.now(UTC),
                    author=AuthorInfo(source_id="U1", display_name="Someone"),
                    content_raw=RAW,
                    content_clean=f"- {RAW}",
                    access_control=AccessControl(
                        visibility=AccessVisibility.PUBLIC, acl_type=AclType.CUSTOM, is_public=True
                    ),
                    connection="slack:T-redact",
                )
            )
            await store.link_context_to_event(tenant, stored, chat_event)
            await conn.execute(
                insert(ASTNode).values(
                    id=node,
                    tenant_id=tenant,
                    repo_identifier=REPO,
                    file_path=FILE,
                    symbol_name=SYMBOL,
                    node_type="function",
                    merkle_hash="h",
                )
            )
            for ctx in (stored, legacy):
                excerpt = stored_copy if ctx == stored else None
                await store.link_context_to_event(tenant, ctx, event, excerpt=excerpt)
                await conn.execute(
                    insert(ASTContextEdge).values(
                        ast_node_id=node, canonical_id=ctx, tenant_id=tenant
                    )
                )
    finally:
        await engine.dispose()
    return stored, legacy


async def _cleanup(tenant: UUID) -> None:
    engine = create_async_engine(ADMIN_URL)
    try:
        async with app_bypass_rls_context(engine) as conn:
            for model in (AuditLogEntry, ASTContextEdge, ASTNode, ContextEventLink, ModelUsage):
                await conn.execute(delete(model).where(model.tenant_id == tenant))
            await conn.execute(delete(events_table).where(events_table.c.tenant_id == tenant))
    finally:
        await engine.dispose()
    await delete_contexts(tenant)


@pytest_asyncio.fixture
async def world() -> AsyncGenerator[World, None]:
    tenant = uuid4()
    reader, admin, plain = (f"chk_{secrets.token_urlsafe(24)}" for _ in range(3))
    specs = [
        KeySpec(reader, tenant, user_email=f"reader-{tenant}@redact.test"),
        KeySpec(admin, tenant, admin=True),
        KeySpec(plain, tenant),
    ]
    try:
        async with seeded_keys(specs):
            stored, legacy = await _seed(tenant)
            role, url = await create_app_role()
            engine = create_checkcord_engine(url)
            try:
                with app_database_url(url):
                    yield World(tenant, reader, admin, plain, stored, legacy, engine)
            finally:
                await engine.dispose()
                await drop_app_role(role)
    finally:
        await _cleanup(tenant)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mcp(client: TestClient, token: str, tool: str, args: dict[str, Any]) -> str:
    res = client.post(
        "/mcp",
        headers={**_auth(token), "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        },
    )
    assert res.status_code == 200, res.text
    body: str = res.json()["result"]["content"][0]["text"]
    assert "error" not in json.loads(body), body
    return body


async def _row(record_id: UUID) -> Any:
    engine = create_async_engine(ADMIN_URL)
    try:
        async with app_bypass_rls_context(engine) as conn:
            t = CanonicalContext.__table__.c
            return (
                await conn.execute(
                    select(t.summary_redacted, t.redaction_version, t.redaction_pending).where(
                        t.id == record_id
                    )
                )
            ).one()
    finally:
        await engine.dispose()


async def _audit(tenant: UUID, action: str) -> list[Any]:
    engine = create_async_engine(ADMIN_URL)
    try:
        async with tenant_context(engine, tenant) as conn:
            t = AuditLogEntry.__table__.c
            return list(
                (
                    await conn.execute(
                        select(AuditLogEntry.__table__).where(
                            (t.tenant_id == tenant) & (t.action == action)
                        )
                    )
                )
                .mappings()
                .all()
            )
    finally:
        await engine.dispose()


async def test_every_api_and_mcp_read_path_serves_redacted_text(
    world: World, model_calls: list[str]
) -> None:
    with TestClient(app) as client:
        bodies = {
            "mcp.get_dependency_flow": _mcp(
                client, world.reader, "get_dependency_flow", {"file_path": FILE}
            ),
            "mcp.view_active_decisions": _mcp(
                client, world.reader, "view_active_decisions", {"repo_identifier": REPO}
            ),
            "mcp.search_conceptual_context": _mcp(
                client, world.reader, "search_conceptual_context", {"query": "charge rule"}
            ),
        }
        for path in (
            f"/api/v1/graph/context?symbol={SYMBOL}",
            f"/api/v1/graph/impact?symbol={SYMBOL}",
            f"/api/v1/graph/decisions/active?repo_identifier={REPO}",
            f"/api/v1/graph/network?repo_identifier={REPO}",
            f"/api/v1/graph/node/context:{world.stored}?repo_identifier={REPO}",
            f"/api/v1/graph/node/context:{world.legacy}?repo_identifier={REPO}",
        ):
            res = client.get(path, headers=_auth(world.reader))
            assert res.status_code == 200, (path, res.text)
            bodies[path] = res.text
        raw = client.get(
            f"/api/v1/admin/raw-content/context:{world.stored}?repo_identifier={REPO}",
            headers=_auth(world.admin),
        )

    for name, body in bodies.items():
        assert _leaks(body) == [], name
    decisions = json.loads(bodies["mcp.view_active_decisions"])
    [served] = [d for d in decisions["decisions"] if d["id"] == str(world.stored)]
    [update_served] = served["updates"]
    assert "[REDACTED:TOKEN]" in update_served["text"] and _leaks(update_served["text"]) == []
    for name in list(bodies)[1:]:
        assert "[REDACTED:TOKEN]" in bodies[name], name
        assert "[REDACTED:EMAIL]" in bodies[name] and "[REDACTED:PHONE]" in bodies[name], name
    assert model_calls == []

    assert raw.status_code == 200, raw.text
    assert RAW in raw.text and "summary_redacted" not in raw.text

    assert (await _row(world.legacy)).summary_redacted is None
    [context_read] = await _audit(world.tenant, "graph.context")
    assert context_read["result_count"] == 2 and context_read["redacted_count"] == 2


async def test_a_secret_only_the_model_found_reaches_no_reader(world: World) -> None:
    engine = create_async_engine(ADMIN_URL)
    follow_up = uuid4()
    try:
        async with tenant_context(engine, world.tenant) as conn:
            store = PostgresEventStore(conn)
            await store.save_event(
                CanonicalEvent(
                    event_id=follow_up,
                    tenant_id=world.tenant,
                    source_platform=SourcePlatform.SLACK,
                    source_type=SourceType.MESSAGE,
                    source_id=f"msg-{follow_up}",
                    happened_at=datetime.now(UTC),
                    author=AuthorInfo(source_id="U2", display_name="Someone else"),
                    content_raw=f"Still works: {MODEL_ONLY}",
                    content_clean=f"Still works: {MODEL_ONLY}",
                    access_control=PUBLIC,
                    connection="slack:T-redact",
                )
            )
            await store.link_context_to_event(world.tenant, world.stored, follow_up)
    finally:
        await engine.dispose()

    paths = [
        f"/api/v1/graph/node/context:{world.stored}?repo_identifier={REPO}",
        f"/api/v1/graph/node/file:{FILE}?repo_identifier={REPO}",
        f"/api/v1/graph/network?repo_identifier={REPO}",
        f"/api/v1/graph/context?symbol={SYMBOL}",
    ]
    with TestClient(app) as client:
        bodies = {p: client.get(p, headers=_auth(world.reader)) for p in paths}
        bodies["mcp"] = _mcp(client, world.reader, "get_dependency_flow", {"file_path": FILE})
        raw = client.get(
            f"/api/v1/admin/raw-content/context:{world.stored}?repo_identifier={REPO}",
            headers=_auth(world.admin),
        )

    for name, res in bodies.items():
        body = res if isinstance(res, str) else res.text
        assert MODEL_ONLY not in body and "Still works" not in body, name
    node = bodies[paths[0]].json()
    assert str(follow_up) in {s["event_id"] for s in node["sources"]}
    assert all("content_clean" not in s for s in node["sources"])
    assert MODEL_ONLY in raw.text


async def test_pr_comment_and_context_files_serve_redacted_text(
    world: World,
    model_calls: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakePullRequest()
    judge = FakeJudge()
    payload = PRContextCheckedPayload(
        repository_full_name=REPO,
        pull_request_number=7,
        head_sha="cafe7",
        tenant_id=world.tenant,
        affected_contexts=[
            {
                "symbol_name": SYMBOL,
                "file_path": FILE,
                "node_type": "function",
                "change_type": "modified",
                "canonical_ids": [str(world.stored), str(world.legacy)],
            }
        ],
    )
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    worker = PRStatusWorker(redis_client=redis, db_engine=world.engine, llm_client=judge)
    with (
        patch("checkcord.workers.pr_status_reporter.GitHubRestClient", return_value=client),
        patch("checkcord.workers.pr_status_reporter.github_token_for", AsyncMock(return_value="x")),
        patch(
            "checkcord.workers.pr_status_reporter.resolve_repo_audience",
            AsyncMock(return_value=RepoAudience()),
        ),
    ):
        await worker._process_message(
            "ci.pr.context_checked", "g", "1-1", {"payload": payload.model_dump_json()}
        )
    comment = client.body
    assert _leaks(comment) == []
    assert "[REDACTED:EMAIL]" in comment and "[REDACTED:TOKEN]" in comment
    assert all(_leaks(text) == [] for text, _ in judge.calls)
    assert "  - Latest update (" in comment

    [check] = await _audit(world.tenant, audit.PR_CHECK)
    assert check["params"]["flagged"] == 2 and check["params"]["blocked"] is False
    assert check["redacted_count"] == 2

    async with tenant_context(world.engine, world.tenant) as conn:
        compiled = await ContextFormatterService().compile_for_repo(
            world.tenant, REPO, PostgresEventStore(conn), RepoAudience(), conn=conn
        )
    assert set(compiled.context_ids) == {str(world.stored), str(world.legacy)}
    assert _leaks(compiled.architecture_md) == []
    assert "[REDACTED:TOKEN]" in compiled.architecture_md
    assert "#### Updates" in compiled.architecture_md

    (tmp_path / ".context").mkdir()
    (tmp_path / ".context" / "ARCHITECTURE.md").write_text(LEGACY)
    monkeypatch.chdir(tmp_path)
    served = await DynamicContextCache().get_context_string()
    assert served is not None and _leaks(served) == []
    assert model_calls == []


async def test_guard_sees_a_real_chat_model_call(model_calls: list[str]) -> None:
    model = ChatOpenAI(model="gpt-4o-mini", api_key="test-" + uuid4().hex, max_retries=0)
    stored = await write_side.redact_for_storage(f"mail {EMAIL}", llm_client=model)
    assert model_calls and stored.pending
    assert stored.text == "mail [REDACTED:EMAIL]"


async def test_failed_model_pass_is_stored_regex_only_and_retried_by_the_sweep(
    world: World,
) -> None:
    first = await write_side.redact_for_storage(RAW, llm_client=_down())
    assert first.pending and MODEL_ONLY in first.text and _leaks(first.text) == [MODEL_ONLY]
    async with tenant_context(world.engine, world.tenant) as conn:
        record = await PostgresEventStore(conn).save_canonical_context(
            tenant_id=world.tenant,
            summary=RAW,
            confidence_score=7.0,
            access_control=PUBLIC,
            redaction=first,
        )
    assert (await _row(record)).redaction_pending is True

    await write_side.sweep_pending(world.engine, llm_client=_down())
    assert (await _row(record)).redaction_pending is True

    await write_side.sweep_pending(world.engine, llm_client=_model(FINDS_PASSWORD))
    after = await _row(record)
    assert after.redaction_pending is False
    assert _leaks(after.summary_redacted) == []
    assert after.redaction_version == write_side.current_version()


async def test_recompute_upgrades_old_versions_and_resumes(world: World) -> None:
    current = write_side.current_version()
    async with tenant_context(world.engine, world.tenant) as conn:
        store = PostgresEventStore(conn)
        old = await store.save_canonical_context(
            tenant_id=world.tenant,
            summary=RAW,
            confidence_score=5.0,
            redaction=StoredRedaction("old copy", "0:old-model"),
        )
        untouched = await store.save_canonical_context(
            tenant_id=world.tenant,
            summary=RAW,
            confidence_score=5.0,
            redaction=StoredRedaction("current copy", current),
        )

    llm = _model(FINDS_PASSWORD)
    runs = write_side.recompute(world.engine, [world.tenant], batch_size=1, llm_client=llm)
    first = await anext(runs)
    await runs.aclose()
    assert first.updated == 1 and first.pending == 0

    rest = [
        p
        async for p in write_side.recompute(
            world.engine, [world.tenant], batch_size=1, llm_client=llm
        )
    ]
    assert [p.updated for p in rest] == [1]
    for record in (world.stored, world.legacy, old):
        row = await _row(record)
        assert row.redaction_version == current and row.redaction_pending is False
        assert _leaks(row.summary_redacted) == []
    assert (await _row(untouched)).summary_redacted == "current copy"


async def test_usage_endpoint_and_cli_report_counts_and_need_an_admin_key(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage.drain()
    with usage.for_tenant(world.tenant):
        usage.record(usage.CHAT, "meeting_adr", "m-chat", latency_s=0.2, input_tokens=1000)
        usage.record(
            usage.CHAT, "meeting_adr", "m-chat", latency_s=0.4, input_tokens=500, output_tokens=50
        )
        usage.record(usage.CHAT, "meeting_adr", "m-chat", latency_s=0.3, failed=True)
        usage.record(usage.EMBEDDING, "search", "m-embed", latency_s=0.1)
    assert await usage.flush(world.engine) == 2

    text_columns = {c.name for c in ModelUsage.__table__.c if c.type.python_type is str}
    assert text_columns == {"kind", "purpose", "model"}

    monkeypatch.setattr(get_settings(), "model_prices", {"m-chat": (2.0, 10.0)})
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/usage?days=1", headers=_auth(world.admin))
        assert client.get("/api/v1/admin/usage", headers=_auth(world.plain)).status_code == 403
        assert client.get("/api/v1/admin/usage", headers=_auth(world.reader)).status_code == 403
        assert client.get("/api/v1/admin/usage").status_code == 401
    assert res.status_code == 200, res.text
    body = res.json()
    rows = {(r["kind"], r["model"]): r for r in body["rows"]}
    chat = rows[(usage.CHAT, "m-chat")]
    assert (chat["calls"], chat["failures"]) == (3, 1)
    assert (chat["input_tokens"], chat["output_tokens"]) == (1500, 50)
    assert chat["avg_latency_ms"] == 300
    assert chat["estimated_cost"] == pytest.approx(0.0035)
    embed = rows[(usage.EMBEDDING, "m-embed")]
    assert embed["calls"] == 1 and embed["estimated_cost"] is None
    assert body["totals"]["calls"] == 4
    assert body["totals"]["estimated_cost"] == pytest.approx(0.0035)


async def test_stats_endpoint_counts_activity_and_needs_an_admin_key(world: World) -> None:
    async with tenant_context(world.engine, world.tenant, include_held=True) as conn:
        store = PostgresEventStore(conn)
        await store.save_canonical_context(
            tenant_id=world.tenant,
            summary="held rule",
            confidence_score=5.0,
            held_connection="jira:held",
        )
        failed = CanonicalEvent(
            tenant_id=world.tenant,
            source_platform=SourcePlatform.JIRA,
            source_type=SourceType.TICKET_CREATED,
            source_id="PROJ-1",
            happened_at=datetime.now(UTC),
            author=AuthorInfo(source_id="U2"),
            content_raw="r",
            content_clean="r",
            access_control=AccessControl(is_public=True),
            connection="jira:held",
        )
        await store.save_event(failed)
        await conn.execute(
            update(events_table)
            .where(events_table.c.event_id == failed.event_id)
            .values(pipeline_state="failed")
        )
        for source_id in ("chat-message:C1:1", "chat-message:C1:2", "chat-tree:t1:0"):
            await store.save_event(
                CanonicalEvent(
                    tenant_id=world.tenant,
                    source_platform=SourcePlatform.SLACK,
                    source_type=SourceType.MESSAGE,
                    source_id=source_id,
                    happened_at=datetime.now(UTC),
                    author=AuthorInfo(source_id="U2"),
                    content_raw=source_id,
                    content_clean=source_id,
                    access_control=AccessControl(is_public=True),
                )
            )
        for flagged, conflicts, blocked, error in (
            (2, 2, True, False),
            (1, 0, False, False),
            (0, 0, False, True),
        ):
            await audit.record(
                conn,
                world.tenant,
                audit.Actor(channel=audit.API),
                audit.PR_CHECK,
                params={
                    "flagged": flagged,
                    "conflicts": conflicts,
                    "blocked": blocked,
                    "error": error,
                },
            )

    with TestClient(app) as client:
        for _ in range(2):
            assert client.get(
                f"/api/v1/graph/context?symbol={SYMBOL}", headers=_auth(world.reader)
            ).is_success
        res = client.get("/api/v1/admin/stats?days=3", headers=_auth(world.admin))
        assert client.get("/api/v1/admin/stats", headers=_auth(world.plain)).status_code == 403
        assert client.get("/api/v1/admin/stats").status_code == 401
    assert res.status_code == 200, res.text
    body = res.json()

    events = {(e["source"], e["messages"], e["events"]) for e in body["events"]}
    assert events == {("slack", 2, 2), ("jira", 0, 1)}
    records = {r["type"]: (r["live"], r["held"]) for r in body["records"]}
    assert records == {"adr": (1, 0), "constraint": (1, 0), "general": (0, 1)}
    assert body["events_unfinished"] == {"pending": 0, "failed": 1}
    assert isinstance(body["dead_letters"], dict)
    [today] = body["agent_reads"]
    assert (today["reads"], today["people"]) == (2, 1)
    assert body["people_reading"] == 1
    assert body["pr_checks"] == {
        "posted": 3,
        "skipped": 0,
        "flagged": 2,
        "conflicts": 1,
        "blocked": 1,
        "errors": 1,
    }
    last = {c["connection"]: c["last_event_at"] for c in body["connections"]}
    assert set(last) >= {"slack:T-redact", "jira:held"}
    assert all(last[k] for k in ("slack:T-redact", "jira:held"))


async def test_unknown_tenant_usage_is_dropped_without_a_default(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "default_tenant_id", None)
    usage.drain()
    usage.record(usage.CHAT, "x", "m-none")
    assert await usage.flush(world.engine) == 0
    engine = create_async_engine(ADMIN_URL)
    try:
        async with app_bypass_rls_context(engine) as conn:
            found = await conn.scalar(
                text("SELECT count(*) FROM model_usage WHERE model = 'm-none'")
            )
    finally:
        await engine.dispose()
    assert found == 0
