"""Tests for get_active_contexts_for_repo_compilation's two sources:
AST-edge-linked contexts, and scope-eligible contexts whose type is not
"general" (a meeting-derived ADR, constraint or risk has no code symbol to
attach an AST edge to).
"""

from uuid import uuid4

import pytest
from sqlalchemy import text

from checkcord.core.config import get_settings
from checkcord.storage.postgres import (
    PostgresEventStore,
    create_checkcord_engine,
    tenant_context,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_typed_scope_eligible_context_appears_without_ast_edge() -> None:
    """A repo-scoped ADR with zero AST edges must still surface for that repo."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    repo_id = "acme/service-a"

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Decision: adopt Postgres",
                confidence_score=8.0,
                scope_repo_identifier=repo_id,
                context_type="adr",
            )

            results = await store.get_active_contexts_for_repo_compilation(tenant_id, repo_id)
            summaries = {r["summary"] for r in results}
            assert summaries == {"Decision: adopt Postgres"}
            assert results[0]["context_type"] == "adr"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_repo_scopes"))
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_general_typed_scope_eligible_context_without_ast_edge_is_excluded() -> None:
    """A repo-scoped but still-general (untyped) context with no AST edge
    must NOT be swept in -- only typed (non-general) contexts get the new
    scope-eligible source; general contexts still require an AST edge,
    exactly as before this task (avoids flooding the compiled doc)."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    repo_id = "acme/service-a"

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Untyped, repo-scoped, no code link",
                confidence_score=8.0,
                scope_repo_identifier=repo_id,
            )

            results = await store.get_active_contexts_for_repo_compilation(tenant_id, repo_id)
            assert results == []
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_repo_scopes"))
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_org_global_typed_context_appears_for_every_repo() -> None:
    """An org-global ADR (no repo scope at all) is scope-eligible for any
    repo, matching _scope_filter's own org-global-always-eligible rule."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Org-wide constraint: all services must log to stdout",
                confidence_score=8.0,
                context_type="constraint",
            )

            results_a = await store.get_active_contexts_for_repo_compilation(
                tenant_id, "acme/service-a"
            )
            results_b = await store.get_active_contexts_for_repo_compilation(
                tenant_id, "acme/service-b"
            )
            assert {r["summary"] for r in results_a} == {
                "Org-wide constraint: all services must log to stdout"
            }
            assert {r["summary"] for r in results_b} == {
                "Org-wide constraint: all services must log to stdout"
            }
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_ast_linked_reference_record_is_compiled_only_when_blocking() -> None:
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    repo_id = "acme/service-a"

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Code-linked rule, still general",
                confidence_score=9.0,
            )
            await store.upsert_ast_node_and_check_drift(
                tenant_id=tenant_id,
                repo_identifier=repo_id,
                file_path="src/a.py",
                symbol_name="do_thing",
                node_type="function",
                merkle_hash="hash_v1",
            )
            nodes = await store.find_ast_nodes_by_symbols(tenant_id, ["do_thing"])
            await store.link_ast_to_context(tenant_id, nodes[0]["id"], ctx_id)

            assert await store.get_active_contexts_for_repo_compilation(tenant_id, repo_id) == []
            await conn.execute(
                text("UPDATE canonical_contexts SET severity = 'hard_block' WHERE id = :id"),
                {"id": ctx_id},
            )
            results = await store.get_active_contexts_for_repo_compilation(tenant_id, repo_id)
            assert {r["summary"] for r in results} == {"Code-linked rule, still general"}
            assert results[0]["context_type"] == "general"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM ast_context_edges"))
            await conn.execute(text("DELETE FROM ast_nodes"))
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_context_scoped_to_a_different_repo_is_excluded() -> None:
    """A typed context scoped to repo B must not leak into repo A's compilation."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Risk scoped to service-b only",
                confidence_score=7.0,
                scope_repo_identifier="acme/service-b",
                context_type="risk",
            )

            results = await store.get_active_contexts_for_repo_compilation(
                tenant_id, "acme/service-a"
            )
            assert results == []
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_repo_scopes"))
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()
