"""`checkcord demo`: the sample story goes through the real pipeline and comes out again."""

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app_role_helpers import app_database_url, create_app_role, drop_app_role
from key_helpers import KeySpec, seeded_keys
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.core.config import get_settings
from checkcord.pipeline.ast_linker import ASTLinkerWorker
from checkcord.pipeline.clustering import ClusteringWorker
from checkcord.pipeline.consumer import PipelineConsumer
from checkcord.pipeline.linkage import CONTEXTS_CREATED, LinkageWorker
from checkcord.services import demo, mcp_tools
from checkcord.storage import audit
from checkcord.storage.postgres import create_checkcord_engine

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ADMIN_URL = get_settings().database_url
TABLES = (
    "record_replacement_suggestions",
    "ast_context_edges",
    "ast_nodes",
    "context_event_links",
    "canonical_contexts",
    "events",
    "connection_reviews",
    "audience_containers",
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


class NeverSame:
    async def judge_linkage(self, summary: str, content: str) -> str:
        return "different"

    async def embed_text(self, text: str) -> list[float]:
        return [1.0] + [0.0] * 1023


@dataclass
class World:
    tenant_id: UUID
    engine: AsyncEngine
    redis: Redis


async def _admin(sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    engine = create_async_engine(ADMIN_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            result = await conn.execute(text(sql), params or {})
            return list(result.fetchall()) if result.returns_rows else []
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(name="w")
async def world(async_redis_client: Redis) -> AsyncGenerator[World, None]:
    tenant_id = uuid4()
    async with seeded_keys([KeySpec(f"demo_admin_{uuid4().hex}", tenant_id, admin=True)]):
        role, url = await create_app_role()
        engine = create_checkcord_engine(url)
        try:
            with app_database_url(url):
                yield World(tenant_id, engine, async_redis_client)
        finally:
            await engine.dispose()
            await drop_app_role(role)
            for table in (*TABLES, "api_keys", "enterprise_users"):
                await _admin(f"DELETE FROM {table} WHERE tenant_id = :t", {"t": tenant_id})


async def _run_pipeline(w: World, bus: CaptureBus) -> None:
    model = NeverSame()
    consumer = PipelineConsumer(bus, w.engine, embedder=model)
    clustering = ClusteringWorker(bus=bus, engine=w.engine)
    linkage = LinkageWorker(bus=bus, engine=w.engine, llm_client=model)
    linker = ASTLinkerWorker(bus=bus, engine=w.engine)
    for event in bus.take("events.raw.custom"):
        await consumer.process_message(event)
        [pending] = bus.take("events.clustering.pending")
        await clustering.process_message(pending)
        [linking] = bus.take("events.linkage.pending")
        await linkage.process_message(linking)
        for created in bus.take(CONTEXTS_CREATED):
            await linker.process_message(created)


async def _search_as(w: World, user_id: UUID) -> list[str]:
    actor = audit.Actor(channel=audit.MCP, user_id=user_id)
    reply = await mcp_tools.conceptual_search(
        w.engine, w.tenant_id, actor, NeverSame, **demo.SEARCH
    )
    return [r["summary"] for r in json.loads(reply)["results"]]


async def _count(sql: str, tenant_id: UUID) -> int:
    [(n,)] = await _admin(sql, {"t": tenant_id})
    return int(n)


async def test_load_runs_the_story_through_the_pipeline_and_remove_clears_it(w: World) -> None:
    bus = CaptureBus()
    loaded = await demo.load(w.engine, w.redis, w.tenant_id, bus=bus)
    assert len(loaded.event_ids) == len(demo.STORY) == 12
    assert set(loaded.keys) == {"mara", "pell"}
    assert all(token.startswith("chk_") for token in loaded.keys.values())
    assert loaded.code_symbols > 0

    before = await demo.progress(w.engine, w.tenant_id, loaded.event_ids)
    assert before.linked == 0 and not before.finished
    await _run_pipeline(w, bus)
    after = await demo.wait(w.engine, w.tenant_id, loaded.event_ids, seconds=0)
    assert after.finished and after.linked == 12 and after.failed == 0

    t = w.tenant_id
    rows = await _admin(
        "SELECT summary, held_connection, access_control FROM canonical_contexts "
        "WHERE tenant_id = :t",
        {"t": t},
    )
    assert len(rows) == 12
    assert all(summary.startswith(demo.MARK) for summary, _, _ in rows)
    assert sum(held == "custom:sample-vendor-wiki" for _, held, _ in rows) == 2
    assert await _count(
        "SELECT count(*) FROM canonical_contexts WHERE tenant_id = :t AND confidence_score > 0", t
    ) == len(rows)
    [(signer,)] = await _admin(
        "SELECT summary_redacted FROM canonical_contexts WHERE tenant_id = :t "
        "AND summary LIKE '%rotate_webhook_signer%'",
        {"t": t},
    )
    assert "rotate_webhook_signer" in signer
    restricted = [
        acl for summary, _, acl in rows if "#security-review" in summary or "HMAC" in summary
    ]
    assert len(restricted) == 2
    members = {str(loaded.people["mara"]), str(loaded.people["juno"])}
    assert all(
        not acl["is_public"] and set(acl["allowed_identity_ids"]) == members for acl in restricted
    )
    assert await _count("SELECT count(*) FROM audience_containers WHERE tenant_id = :t", t) == 2
    assert await _count("SELECT count(*) FROM ast_context_edges WHERE tenant_id = :t", t) > 0
    assert await demo.is_loaded(w.engine, t)
    [(old, new, basis)] = await _admin(
        "SELECT o.summary, n.summary, s.basis FROM record_replacement_suggestions s "
        "JOIN canonical_contexts o ON o.id = s.record_id "
        "JOIN canonical_contexts n ON n.id = s.replacement_id WHERE s.tenant_id = :t",
        {"t": t},
    )
    assert "nightly batch job" in old and "supersedes" in new and basis == "stated"

    mara = await _search_as(w, loaded.people["mara"])
    pell = await _search_as(w, loaded.people["pell"])
    assert any("#security-review" in s for s in mara)
    assert pell and not any("#security-review" in s or "HMAC" in s for s in pell)

    with pytest.raises(demo.SampleDataExistsError):
        await demo.load(w.engine, w.redis, t, bus=CaptureBus())

    removed = await demo.remove(w.engine, w.redis, t)
    assert removed.events == 12 and removed.records == 12
    assert removed.people == 4 and removed.keys == 2 and removed.audiences == 2
    for table in TABLES[:-1] + ("enterprise_users",):
        assert await _count(f"SELECT count(*) FROM {table} WHERE tenant_id = :t", t) == 0, table
    assert (
        await _count(
            "SELECT count(*) FROM api_keys WHERE tenant_id = :t AND name LIKE 'sample:%'", t
        )
        == 0
    )
    actions = {
        a
        for (a,) in await _admin(
            "SELECT action FROM audit_log_entries WHERE tenant_id = :t", {"t": t}
        )
    }
    assert {"sample.loaded", "sample.removed"} <= actions
    assert not await demo.is_loaded(w.engine, t)


async def test_remove_with_nothing_loaded_is_harmless(w: World) -> None:
    removed = await demo.remove(w.engine, w.redis, w.tenant_id)
    assert (removed.events, removed.records, removed.people, removed.code_symbols) == (0, 0, 0, 0)
