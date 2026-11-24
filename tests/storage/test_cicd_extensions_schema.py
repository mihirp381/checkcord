"""Real-database tests for PostgresEventStore.upsert_workflow_definition and
.create_ci_failure_context_link: upsert conflict targets, FK constraints and
RLS against the live schema.
"""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.core.config import get_settings
from checkcord.storage.postgres import PostgresEventStore, tenant_context

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_upsert_workflow_definition_insert_then_refresh() -> None:
    """A second upsert for the same (tenant, repo, workflow_path) updates in
    place and does not create a second row.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)

            await store.upsert_workflow_definition(
                tenant_id=tenant_id,
                repo_identifier="owner/repo",
                workflow_path=".github/workflows/ci.yml",
                name="CI",
                jobs=[{"job_id": "test", "name": None, "needs": [], "steps": []}],
                commit_sha="sha1",
            )

            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT name, commit_sha, jobs FROM workflow_definitions "
                            "WHERE tenant_id = :tid AND workflow_path = :path"
                        ),
                        {"tid": tenant_id, "path": ".github/workflows/ci.yml"},
                    )
                )
                .mappings()
                .fetchall()
            )
            assert len(rows) == 1
            assert rows[0]["name"] == "CI"
            assert rows[0]["commit_sha"] == "sha1"

            # Refresh with a new commit -- must update the SAME row.
            await store.upsert_workflow_definition(
                tenant_id=tenant_id,
                repo_identifier="owner/repo",
                workflow_path=".github/workflows/ci.yml",
                name="CI (renamed)",
                jobs=[{"job_id": "test", "name": None, "needs": [], "steps": []}],
                commit_sha="sha2",
            )

            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT name, commit_sha FROM workflow_definitions "
                            "WHERE tenant_id = :tid AND workflow_path = :path"
                        ),
                        {"tid": tenant_id, "path": ".github/workflows/ci.yml"},
                    )
                )
                .mappings()
                .fetchall()
            )
            assert len(rows) == 1  # still exactly one row, not two
            assert rows[0]["name"] == "CI (renamed)"
            assert rows[0]["commit_sha"] == "sha2"
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM workflow_definitions WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
        await engine.dispose()


async def test_create_ci_failure_context_link_idempotent() -> None:
    """Idempotent on (ci_event_id, canonical_id, file_path): a second call
    with the same key must return False (no duplicate row), not raise."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    tenant_id = uuid4()
    context_id = uuid4()
    ci_event_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="A context affected by a CI failure",
                confidence_score=0.5,
                id=context_id,
            )

            inserted_first = await store.create_ci_failure_context_link(
                tenant_id=tenant_id,
                ci_event_id=ci_event_id,
                canonical_id=context_id,
                file_path="src/main.py",
            )
            assert inserted_first is True

            inserted_second = await store.create_ci_failure_context_link(
                tenant_id=tenant_id,
                ci_event_id=ci_event_id,
                canonical_id=context_id,
                file_path="src/main.py",
            )
            assert inserted_second is False

            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT file_path FROM ci_failure_context_links "
                            "WHERE ci_event_id = :ci_event_id"
                        ),
                        {"ci_event_id": ci_event_id},
                    )
                )
                .mappings()
                .fetchall()
            )
            assert len(rows) == 1  # still exactly one row despite two calls
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM ci_failure_context_links WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE id = :cid"), {"cid": context_id}
            )
        await engine.dispose()


async def test_create_ci_failure_context_link_with_ast_node() -> None:
    """Links carrying a real ast_node_id must satisfy the FK to ast_nodes."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    tenant_id = uuid4()
    context_id = uuid4()
    ci_event_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="A context with a real AST node",
                confidence_score=0.5,
                id=context_id,
            )
            await store.upsert_ast_node_and_check_drift(
                tenant_id=tenant_id,
                repo_identifier="owner/repo",
                file_path="src/main.py",
                symbol_name="handler",
                node_type="function_definition",
                merkle_hash="hash1",
            )
            nodes = await store.find_ast_nodes_by_symbols(tenant_id, ["handler"])
            assert len(nodes) == 1
            ast_node_id = nodes[0]["id"]

            inserted = await store.create_ci_failure_context_link(
                tenant_id=tenant_id,
                ci_event_id=ci_event_id,
                canonical_id=context_id,
                file_path="src/main.py",
                ast_node_id=ast_node_id,
            )
            assert inserted is True

            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT ast_node_id FROM ci_failure_context_links "
                            "WHERE ci_event_id = :ci_event_id"
                        ),
                        {"ci_event_id": ci_event_id},
                    )
                )
                .mappings()
                .fetchall()
            )
            assert rows[0]["ast_node_id"] == ast_node_id
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM ci_failure_context_links WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
            await conn.execute(
                text("DELETE FROM ast_nodes WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE id = :cid"), {"cid": context_id}
            )
        await engine.dispose()
