"""The three-way linking verdict, update excerpts, replacement notes and who sees them."""

import itertools
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app_role_helpers import app_database_url, create_app_role, drop_app_role
from fastapi.testclient import TestClient
from key_helpers import KeySpec, everyone_reads, seeded_keys
from langchain_core.messages import AIMessage
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.core.config import get_settings
from checkcord.core.exceptions import LinkageAdjudicationError
from checkcord.llm.client import LLMClient
from checkcord.main import app
from checkcord.models.common import AccessControl, AuthorInfo, CodeMetadata
from checkcord.models.enums import AccessVisibility, AclType, SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.pipeline import redaction as write_side
from checkcord.pipeline.ast_linker import ASTLinkerWorker
from checkcord.pipeline.clustering import ClusteringWorker
from checkcord.pipeline.consumer import PipelineConsumer
from checkcord.pipeline.linkage import LinkageWorker
from checkcord.pipeline.redaction import RedactedEntity, RedactionOutput, current_version
from checkcord.services import code_links, purge
from checkcord.storage import audit
from checkcord.storage.postgres import PostgresEventStore, create_checkcord_engine, tenant_context

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ADMIN_URL = get_settings().database_url
START = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
LIVE = "jira:linking.example.net"
REPO = "acme/linking"
CHANNEL = "#billing-updates"
CENTS = "Decision: billing stores money as integer cents; never floats."
_AXES = itertools.count()
TABLES = (
    "ast_context_edges",
    "ast_nodes",
    "record_replacement_suggestions",
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
        # Close enough to cluster, never close enough to count as a restatement.
        vector = [1.0] + [0.0] * 1023
        vector[next(_AXES) % 1000 + 1] = 0.5
        return vector


class Replies:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)

    async def ainvoke(self, messages: Any) -> AIMessage:
        return AIMessage(content=self.replies.pop(0))


@dataclass
class World:
    tenant_id: UUID
    engine: AsyncEngine
    redis: Redis
    tokens: dict[str, str]
    people: dict[str, UUID]


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
    tokens = {who: f"linking_{who}_{uuid4().hex}" for who in ("a", "b", "admin")}
    emails = {who: f"{who}-{tenant_id}@linking.test" for who in ("a", "b")}
    specs = [
        KeySpec(tokens["a"], tenant_id, user_email=emails["a"]),
        KeySpec(tokens["b"], tenant_id, user_email=emails["b"]),
        KeySpec(tokens["admin"], tenant_id, admin=True),
    ]
    async with seeded_keys(specs) as users:
        await everyone_reads(tenant_id, LIVE)
        people = {who: users[email] for who, email in emails.items()}
        role, url = await create_app_role()
        engine = create_checkcord_engine(url)
        try:
            with app_database_url(url):
                yield World(tenant_id, engine, async_redis_client, tokens, people)
        finally:
            await engine.dispose()
            await drop_app_role(role)
            for table in TABLES:
                await _admin(f"DELETE FROM {table} WHERE tenant_id = :t", {"t": tenant_id})


def _event(
    w: World,
    n: int,
    content: str,
    *,
    kind: str = "adr",
    repo: str | None = None,
    only: UUID | tuple[UUID, ...] | None = None,
    slack: bool = False,
    author: str | None = None,
) -> CanonicalEvent:
    if only is None:
        access = AccessControl(is_public=True)
    else:
        access = AccessControl(
            is_public=False,
            visibility=AccessVisibility.PRIVATE,
            acl_type=AclType.CUSTOM,
            allowed_identity_ids=list(only) if isinstance(only, tuple) else [only],
        )
    return CanonicalEvent(
        tenant_id=w.tenant_id,
        source_platform=SourcePlatform.SLACK if slack else SourcePlatform.JIRA,
        source_type=SourceType.MESSAGE if slack else SourceType.TICKET_CREATED,
        source_id=f"linking:{n}:{uuid4().hex[:6]}",
        happened_at=START + timedelta(days=n),
        author=AuthorInfo(source_id=author or "someone", email=author),
        content_raw=content,
        content_clean=content,
        access_control=access,
        audience_container=CHANNEL if slack else None,
        connection=LIVE if only is None else None,
        code_metadata=CodeMetadata(repository=repo, branch="main") if repo else None,
        metadata={"context_type": kind},
    )


async def _ingest(
    w: World, model: Any, *events: CanonicalEvent, bus: CaptureBus | None = None
) -> list[UUID]:
    bus = bus or CaptureBus()
    embedder = model if hasattr(model, "embed_text") else Model()
    consumer = PipelineConsumer(bus, w.engine, embedder=embedder)
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


async def _links(w: World) -> dict[UUID, Any]:
    rows = await _admin(
        "SELECT event_id, canonical_id, excerpt_redacted, excerpt_version, excerpt_pending "
        "FROM context_event_links WHERE tenant_id = :t",
        {"t": w.tenant_id},
    )
    return {r.event_id: r for r in rows}


async def _suggestions(w: World) -> list[Any]:
    return await _admin(
        "SELECT record_id, replacement_id, basis FROM record_replacement_suggestions "
        "WHERE tenant_id = :t",
        {"t": w.tenant_id},
    )


def _decisions(w: World, who: str) -> dict[str, dict[str, Any]]:
    with TestClient(app) as client:
        res = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {w.tokens[who]}",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "view_active_decisions", "arguments": {}},
            },
        )
    assert res.status_code == 200, res.text
    body = json.loads(res.json()["result"]["content"][0]["text"])
    return {d["id"]: d for d in body["decisions"]}


def _detail_as(w: World, who: str, record: UUID) -> dict[str, Any]:
    with TestClient(app) as client:
        res = client.get(
            f"/api/v1/graph/node/context:{record}?repo_identifier={REPO}",
            headers={"Authorization": f"Bearer {w.tokens[who]}"},
        )
    assert res.status_code == 200, res.text
    body: dict[str, Any] = res.json()
    return body


def _detail(w: World, record: UUID) -> dict[str, Any]:
    with TestClient(app) as client:
        res = client.get(
            f"/api/v1/admin/records/{record}",
            headers={"Authorization": f"Bearer {w.tokens['admin']}"},
        )
    assert res.status_code == 200, res.text
    body: dict[str, Any] = res.json()
    return body


async def _score(record: UUID) -> float:
    [(score,)] = await _admin(
        "SELECT confidence_score FROM canonical_contexts WHERE id = :r", {"r": record}
    )
    return float(score)


async def test_same_links_and_stores_an_excerpt(w: World) -> None:
    origin, update = _event(w, 1, CENTS), _event(w, 5, f"{CENTS} This also applies to refunds.")
    [record] = await _ingest(w, Model(), origin)
    first = await _score(record)
    assert first > 0
    [joined] = await _ingest(w, Model("same"), update)
    assert joined == record
    links = await _links(w)
    assert links[origin.event_id].excerpt_redacted is None
    stored = links[update.event_id]
    assert "applies to refunds" in stored.excerpt_redacted
    assert stored.excerpt_version == current_version()
    assert await _score(record) > first

    served = _decisions(w, "a")[str(record)]
    [item] = served["updates"]
    assert "applies to refunds" in item["text"]
    assert item["source"] == {"platform": "jira", "type": "ticket_created"}
    assert item["happened_at"].startswith("2026-04-06")
    assert served["supporting_events"]["count"] == 2
    assert "may_be_replaced_by" not in served
    assert [u["text"] for u in _detail(w, record)["updates"]] == [item["text"]]


async def test_a_restatement_stores_no_excerpt(w: World) -> None:
    origin, again = _event(w, 1, CENTS), _event(w, 2, CENTS.upper().replace(";", ","))
    [record] = await _ingest(w, Model(), origin)
    assert await _ingest(w, Model("same"), again) == [record]
    assert (await _links(w))[again.event_id].excerpt_redacted is None
    assert _decisions(w, "a")[str(record)]["updates"] == []


async def test_changes_starts_a_record_with_one_model_suggestion(w: World) -> None:
    [old] = await _ingest(w, Model(), _event(w, 1, CENTS))
    model = Model("changes")
    [new] = await _ingest(
        w, model, _event(w, 9, "Billing now stores money as decimal strings.", kind="constraint")
    )
    assert new != old and len(model.judged) == 1
    assert await _suggestions(w) == [(old, new, "model")]
    assert (await _links(w)) and all(r.excerpt_redacted is None for r in (await _links(w)).values())
    served = _decisions(w, "a")
    assert served[str(new)] and served[str(old)]["may_be_replaced_by"]["record_id"] == str(new)


@pytest.mark.parametrize(
    ("kind", "repo"), [("risk", None), ("adr", REPO)], ids=["incompatible-type", "other-scope"]
)
async def test_changes_without_a_suggestion(w: World, kind: str, repo: str | None) -> None:
    [old] = await _ingest(w, Model(), _event(w, 1, CENTS))
    model = Model("changes")
    [new] = await _ingest(
        w, model, _event(w, 9, "Floats are now allowed for prices.", kind=kind, repo=repo)
    )
    assert new != old and len(model.judged) == 1
    assert await _suggestions(w) == []


async def test_different_starts_an_unrelated_record(w: World) -> None:
    [old] = await _ingest(w, Model(), _event(w, 1, CENTS))
    other = _event(w, 3, "Decision: invoices are emailed on the first of the month.")
    model = Model("different")
    [new] = await _ingest(w, model, other)
    assert new != old and len(model.judged) == 1
    assert await _suggestions(w) == []
    assert (await _links(w))[other.event_id].excerpt_redacted is None


async def test_an_unparseable_verdict_fails_the_event(w: World) -> None:
    await _ingest(w, Model(), _event(w, 1, CENTS))
    bus = CaptureBus()
    consumer = PipelineConsumer(bus, w.engine, embedder=Model())
    clustering = ClusteringWorker(bus=bus, engine=w.engine)
    client = LLMClient(model=Replies("Maybe.", "SAME or CHANGES"))
    linkage = LinkageWorker(bus=bus, engine=w.engine, llm_client=client)
    event = _event(w, 2, "Billing keeps cents, mostly.")
    await consumer.process_message(event)
    [pending] = bus.take("events.clustering.pending")
    await clustering.process_message(pending)
    [linking] = bus.take("events.linkage.pending")
    with pytest.raises(LinkageAdjudicationError):
        await linkage.process_message(linking)
    assert event.event_id not in await _links(w)
    [(records,)] = await _admin(
        "SELECT count(*) FROM canonical_contexts WHERE tenant_id = :t", {"t": w.tenant_id}
    )
    assert records == 1


async def test_note_only_for_a_reader_who_can_read_the_newer_record(w: World) -> None:
    [old] = await _ingest(w, Model(), _event(w, 1, CENTS))
    newer = _event(w, 9, "Billing now stores money as decimal strings.", only=w.people["a"])
    [new] = await _ingest(w, Model("changes"), newer)
    assert await _suggestions(w) == [(old, new, "model")]

    seen_by_a = _decisions(w, "a")
    note = seen_by_a[str(old)]["may_be_replaced_by"]
    assert note["record_id"] == str(new) and note["basis"] == "model"
    assert note["summary"].startswith("Billing now stores money as decimal strings")
    seen_by_b = _decisions(w, "b")
    assert str(new) not in seen_by_b and "may_be_replaced_by" not in seen_by_b[str(old)]
    assert _detail(w, old)["may_be_replaced_by"]["record_id"] == str(new)


async def test_an_update_from_a_narrower_source_is_shown_only_to_its_readers(w: World) -> None:
    origin = _event(w, 1, CENTS)
    narrow = _event(w, 4, f"{CENTS} Refunds too.", only=w.people["a"])
    [record] = await _ingest(w, Model(), origin)
    assert await _ingest(w, Model("same"), narrow) == [record]
    assert _decisions(w, "b")[str(record)]["updates"] == []
    assert "Refunds too." not in json.dumps(_detail_as(w, "b", record))
    [item] = _decisions(w, "a")[str(record)]["updates"]
    assert "Refunds too." in item["text"]
    assert "Refunds too." in json.dumps(_detail_as(w, "a", record))
    labels = [u["audience"] for u in _detail(w, record)["updates"]]
    assert labels == ["1 people"]


async def _with_update(w: World, author: str | None = None) -> tuple[UUID, CanonicalEvent]:
    [record] = await _ingest(w, Model(), _event(w, 1, CENTS))
    update = _event(w, 4, f"{CENTS} Refunds too.", slack=True, author=author)
    await _ingest(w, Model("same"), update)
    assert (await _links(w))[update.event_id].excerpt_redacted
    return record, update


async def _survives_without(w: World, record: UUID, update: CanonicalEvent) -> None:
    links = await _links(w)
    assert update.event_id not in links
    assert [r.canonical_id for r in links.values()] == [record]
    assert _decisions(w, "a")[str(record)]["updates"] == []


async def test_purging_the_update_source_removes_its_excerpt(w: World) -> None:
    record, update = await _with_update(w)
    await purge.purge(w.engine, w.tenant_id, purge.source("slack", CHANNEL), actor=audit.CLI_ACTOR)
    await _survives_without(w, record, update)


async def test_purging_the_update_event_removes_its_excerpt(w: World) -> None:
    record, update = await _with_update(w)
    await purge.purge(w.engine, w.tenant_id, purge.events([update.event_id]), actor=audit.CLI_ACTOR)
    await _survives_without(w, record, update)


async def test_deleting_the_update_author_removes_their_excerpt(w: World) -> None:
    email = f"b-{w.tenant_id}@linking.test"
    record, update = await _with_update(w, author=email)
    deleted = await purge.delete_person(w.engine, w.tenant_id, actor=audit.CLI_ACTOR, email=email)
    assert deleted is not None and deleted.purged.events == 1
    await _survives_without(w, record, update)


def _finds(spans: list[str]) -> MagicMock:
    structured = AsyncMock()
    structured.ainvoke.return_value = RedactionOutput(
        entities=[RedactedEntity(text=t, entity_type="TOKEN") for t in spans]
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


async def test_sweep_and_recompute_cover_update_excerpts(w: World) -> None:
    secret = "-".join(("hunter2", "refund", "pass"))
    await _ingest(w, Model(), _event(w, 1, CENTS))
    update = _event(w, 4, f"{CENTS} Refunds too; the vault password is {secret}.")
    await _ingest(w, Model("same"), update)
    await _admin(
        "UPDATE context_event_links SET excerpt_pending = true, excerpt_redacted = :t "
        "WHERE event_id = :e",
        {"t": update.content_clean, "e": update.event_id},
    )
    assert await write_side.sweep_pending(w.engine, llm_client=_finds([secret])) >= 1
    swept = (await _links(w))[update.event_id]
    assert secret not in swept.excerpt_redacted and swept.excerpt_pending is False

    await _admin(
        "UPDATE context_event_links SET excerpt_version = 'old', excerpt_redacted = :t "
        "WHERE event_id = :e",
        {"t": update.content_clean, "e": update.event_id},
    )
    steps = [
        s async for s in write_side.recompute(w.engine, [w.tenant_id], llm_client=_finds([secret]))
    ]
    assert any(s.kind == "updates" and s.updated == 1 for s in steps)
    again = (await _links(w))[update.event_id]
    assert again.excerpt_version == current_version() and secret not in again.excerpt_redacted


def _skipped(reason: str) -> float:
    from checkcord.core import metrics

    return metrics.LINKAGE_SKIPPED.labels(reason)._value.get()


async def test_a_public_event_does_not_join_a_restricted_record(w: World) -> None:
    [restricted] = await _ingest(w, Model(), _event(w, 1, CENTS, only=w.people["a"]))
    before = _skipped("public_event")
    model = Model("same")
    [public] = await _ingest(w, model, _event(w, 4, f"{CENTS} Refunds too."))
    assert public != restricted
    assert model.judged == []
    assert _skipped("public_event") == before + 1
    assert str(public) in _decisions(w, "b") and str(restricted) not in _decisions(w, "b")
    assert _decisions(w, "a")[str(restricted)]["updates"] == []


@pytest.mark.parametrize(
    ("record_readers", "event_readers", "joins"),
    [(("a", "b"), ("a",), True), (("a",), ("a", "b"), False), (("a",), ("b",), False)],
    ids=["subset", "wider", "disjoint"],
)
async def test_a_restricted_event_joins_only_a_record_all_its_readers_can_read(
    w: World, record_readers: tuple[str, ...], event_readers: tuple[str, ...], joins: bool
) -> None:
    def readers(names: tuple[str, ...]) -> tuple[UUID, ...]:
        return tuple(w.people[n] for n in names)

    [record] = await _ingest(w, Model(), _event(w, 1, CENTS, only=readers(record_readers)))
    before = _skipped("wider_audience")
    [joined] = await _ingest(
        w, Model("same"), _event(w, 4, f"{CENTS} Refunds too.", only=readers(event_readers))
    )
    assert (joined == record) is joins
    assert _skipped("wider_audience") == before + (0 if joins else 1)
    [(access,)] = await _admin(
        "SELECT access_control FROM canonical_contexts WHERE id = :r", {"r": record}
    )
    assert sorted(access["allowed_identity_ids"]) == sorted(str(u) for u in readers(record_readers))


FILE = "pricing/quote.py"


async def _node(w: World, symbol: str, repo: str = REPO) -> UUID:
    [(node,)] = await _admin(
        "INSERT INTO ast_nodes (tenant_id, repo_identifier, file_path, symbol_name, node_type, "
        "merkle_hash) VALUES (:t, :r, :f, :s, 'function', 'h') RETURNING id",
        {"t": w.tenant_id, "r": repo, "f": FILE, "s": symbol},
    )
    return UUID(str(node))


async def _edges(w: World) -> set[tuple[UUID, UUID]]:
    rows = await _admin(
        "SELECT ast_node_id, canonical_id FROM ast_context_edges WHERE tenant_id = :t",
        {"t": w.tenant_id},
    )
    return {(r.ast_node_id, r.canonical_id) for r in rows}


async def _held(w: World, summary: str) -> UUID:
    async with tenant_context(w.engine, w.tenant_id, include_held=True) as conn:
        return await PostgresEventStore(conn).save_canonical_context(
            tenant_id=w.tenant_id,
            summary=summary,
            confidence_score=1.0,
            access_control={"is_public": True},
            held_connection=LIVE,
        )


def _flow(w: World, who: str) -> dict[str, Any]:
    with TestClient(app) as client:
        res = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {w.tokens[who]}",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_dependency_flow", "arguments": {"file_path": FILE}},
            },
        )
    assert res.status_code == 200, res.text
    body: dict[str, Any] = json.loads(res.json()["result"]["content"][0]["text"])
    return {n["symbol_name"]: n["records"] for n in body["nodes"]}


async def test_a_new_record_links_to_the_symbols_it_names(w: World) -> None:
    quote, entry = await _node(w, "quote_total"), await _node(w, "post_entry")
    bus = CaptureBus()
    [record] = await _ingest(
        w,
        Model(),
        _event(w, 1, "Never use floats in `quote_total`; call ledger.post_entry."),
        bus=bus,
    )
    [created] = bus.take("contexts.created")
    linker = ASTLinkerWorker(bus=bus, engine=w.engine)
    await linker.process_message(created)
    await linker.process_message(created)
    assert await _edges(w) == {(quote, record), (entry, record)}


async def test_an_update_excerpt_links_the_symbols_it_names(w: World) -> None:
    entry = await _node(w, "post_entry")
    [record] = await _ingest(w, Model(), _event(w, 1, CENTS))
    assert await _edges(w) == set()
    await _ingest(w, Model("same"), _event(w, 5, f"{CENTS} ledger.post_entry rounds the same way."))
    assert await _edges(w) == {(entry, record)}


async def test_indexing_a_repository_links_existing_live_records(w: World) -> None:
    [record] = await _ingest(w, Model(), _event(w, 1, f"{CENTS} See `quote_total`."))
    await _ingest(w, Model("same"), _event(w, 5, f"{CENTS} refund_total too."))
    [retired] = await _ingest(w, Model("different"), _event(w, 9, "Retired: `quote_total`."))
    await _held(w, "Held: `quote_total`.")
    await _admin("UPDATE canonical_contexts SET retired_at = now() WHERE id = :r", {"r": retired})
    quote, refund = await _node(w, "quote_total"), await _node(w, "refund_total")
    await _node(w, "quote_total", repo="acme/other")
    assert await code_links.link_repository(w.engine, w.tenant_id, REPO) == 2
    assert await code_links.link_repository(w.engine, w.tenant_id, REPO) == 0
    assert await _edges(w) == {(quote, record), (refund, record)}


async def test_links_of_retired_and_held_records_do_not_count(w: World) -> None:
    quote = await _node(w, "quote_total")
    [live] = await _ingest(w, Model(), _event(w, 1, "Never use floats in `quote_total`."))
    [retired] = await _ingest(w, Model("different"), _event(w, 2, "Old: `quote_total`."))
    held = await _held(w, "Held: `quote_total`.")
    for record in (live, retired, held):
        await _admin(
            "INSERT INTO ast_context_edges (ast_node_id, canonical_id, tenant_id) "
            "VALUES (:n, :r, :t) ON CONFLICT DO NOTHING",
            {"n": quote, "r": record, "t": w.tenant_id},
        )
    await _admin("UPDATE canonical_contexts SET retired_at = now() WHERE id = :r", {"r": retired})

    async with tenant_context(w.engine, w.tenant_id) as conn:
        rows = await PostgresEventStore(conn).find_ast_nodes_by_paths(w.tenant_id, REPO, [FILE])
    assert {r["canonical_id"] for r in rows} == {live}
    assert [r["id"] for r in _flow(w, "a")["quote_total"]] == [str(live)]


async def test_dependency_flow_serves_readable_records_blocking_first(w: World) -> None:
    quote = await _node(w, "quote_total")
    long_text = "Never use floats in `quote_total`. " + "x" * 400
    [advisory] = await _ingest(w, Model(), _event(w, 1, long_text))
    other = Model("different")
    [blocking] = await _ingest(
        w, other, _event(w, 2, "`quote_total` rounds up.", kind="constraint")
    )
    [newest] = await _ingest(w, other, _event(w, 3, "Note on `quote_total`.", kind="risk"))
    [only_a] = await _ingest(
        w, other, _event(w, 4, "Private `quote_total` rule.", kind="risk", only=w.people["a"])
    )
    await code_links.link_repository(w.engine, w.tenant_id, REPO)
    await _admin(
        "UPDATE canonical_contexts SET severity = 'hard_block' WHERE id = :r", {"r": blocking}
    )
    assert {(n, r) for n, r in await _edges(w)} == {
        (quote, r) for r in (advisory, blocking, newest, only_a)
    }

    seen_by_a = _flow(w, "a")["quote_total"]
    assert [r["id"] for r in seen_by_a] == [str(blocking), str(only_a), str(newest), str(advisory)]
    assert seen_by_a[0]["severity"] == "blocking" and seen_by_a[1]["severity"] == "advisory"
    assert seen_by_a[0]["type"] == "constraint"
    assert seen_by_a[3]["text"] == long_text[:240]
    seen_by_b = _flow(w, "b")["quote_total"]
    assert str(only_a) not in json.dumps(seen_by_b)
    assert "Private" not in json.dumps(seen_by_b) and len(seen_by_b) == 3

    reads = await _admin(
        "SELECT user_id, record_ids FROM audit_log_entries "
        "WHERE tenant_id = :t AND action = 'mcp.get_dependency_flow'",
        {"t": w.tenant_id},
    )
    by_reader = {str(r.user_id): sorted(map(str, r.record_ids)) for r in reads}
    assert by_reader[str(w.people["b"])] == sorted(str(r) for r in (blocking, newest, advisory))
    assert str(only_a) in by_reader[str(w.people["a"])]
