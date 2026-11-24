from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from checkcord.core.config import get_settings
from checkcord.storage.models import (
    ASTContextEdge,
    ASTNode,
    CanonicalContext,
    GithubInstallation,
)
from checkcord.storage.postgres import PostgresEventStore, tenant_context
from checkcord.workers.staleness_cron import run_staleness_pruning

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture
async def db_session_and_engine():
    """Setup settings and database connections."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    # Ensure cleanup before and after test
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(text("DELETE FROM ast_context_edges"))
        await conn.execute(text("DELETE FROM ast_nodes"))
        await conn.execute(text("DELETE FROM canonical_contexts"))
        await conn.execute(text("DELETE FROM github_installations"))

    yield async_session, engine

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(text("DELETE FROM ast_context_edges"))
        await conn.execute(text("DELETE FROM ast_nodes"))
        await conn.execute(text("DELETE FROM canonical_contexts"))
        await conn.execute(text("DELETE FROM github_installations"))
    await engine.dispose()


@patch("checkcord.workers.staleness_cron.GitHubRestClient")
async def test_run_staleness_pruning_success(
    mock_client_class: Any, db_session_and_engine: Any
) -> None:
    """Verify that run_staleness_pruning marks old edges as stale, updates last_accessed on touch,
    and skips inactive repos.
    """
    async_session, engine = db_session_and_engine

    # Mock GitHub activity checks
    mock_client = AsyncMock()
    mock_client_class.return_value = mock_client

    # Repo A has recent activity, Repo B does not
    def mock_has_recent_commits(owner, repo, since_days):
        if repo == "repo-active":
            return True
        return False

    mock_client.has_recent_commits.side_effect = mock_has_recent_commits

    tenant_id = uuid4()
    repo_active = "my-org/repo-active"
    repo_inactive = "my-org/repo-inactive"

    # Seed data
    async with async_session() as session:
        # Create GitHub installations
        inst_active = GithubInstallation(
            id=uuid4(),
            repo_identifier=repo_active,
            tenant_id=tenant_id,
            personal_access_token="tok_active",
            webhook_secret="sec",
        )
        inst_inactive = GithubInstallation(
            id=uuid4(),
            repo_identifier=repo_inactive,
            tenant_id=tenant_id,
            personal_access_token="tok_inactive",
            webhook_secret="sec",
        )
        session.add_all([inst_active, inst_inactive])

        # Create Canonical Contexts
        ctx1 = CanonicalContext(
            id=uuid4(),
            tenant_id=tenant_id,
            summary=" PI1: Always encrypt database rows",
        )
        ctx2 = CanonicalContext(
            id=uuid4(),
            tenant_id=tenant_id,
            summary="PI2: Use custom auth",
        )
        ctx_inactive = CanonicalContext(
            id=uuid4(),
            tenant_id=tenant_id,
            summary="PI3: Inactive repo rule",
        )
        session.add_all([ctx1, ctx2, ctx_inactive])

        # Create AST Nodes
        node_old = ASTNode(
            id=uuid4(),
            tenant_id=tenant_id,
            repo_identifier=repo_active,
            file_path="src/old.py",
            symbol_name="encrypt_db",
            node_type="function",
            merkle_hash="hash_old",
        )
        node_new = ASTNode(
            id=uuid4(),
            tenant_id=tenant_id,
            repo_identifier=repo_active,
            file_path="src/new.py",
            symbol_name="auth_custom",
            node_type="function",
            merkle_hash="hash_new",
        )
        node_inactive = ASTNode(
            id=uuid4(),
            tenant_id=tenant_id,
            repo_identifier=repo_inactive,
            file_path="src/main.py",
            symbol_name="inactive_func",
            node_type="function",
            merkle_hash="hash_inactive",
        )
        session.add_all([node_old, node_new, node_inactive])
        await session.commit()

        # Create AST Context Edges with custom timestamps (use raw sql insert or construct
        # via model)
        # Note: to set last_accessed explicitly to past dates, we can execute an UPDATE statement
        edge_old = ASTContextEdge(
            ast_node_id=node_old.id, canonical_id=ctx1.id, tenant_id=tenant_id, status="active"
        )
        edge_new = ASTContextEdge(
            ast_node_id=node_new.id, canonical_id=ctx2.id, tenant_id=tenant_id, status="active"
        )
        edge_inactive = ASTContextEdge(
            ast_node_id=node_inactive.id,
            canonical_id=ctx_inactive.id,
            tenant_id=tenant_id,
            status="active",
        )
        session.add_all([edge_old, edge_new, edge_inactive])
        await session.commit()

        # Manually alter last_accessed dates back in time
        utc_now = datetime.now(UTC)
        old_date = utc_now - timedelta(days=190)
        new_date = utc_now - timedelta(days=10)

        await session.execute(
            text(
                "UPDATE ast_context_edges SET last_accessed = :old_date "
                "WHERE ast_node_id = :node_id"
            ),
            {"old_date": old_date, "node_id": node_old.id},
        )
        await session.execute(
            text(
                "UPDATE ast_context_edges SET last_accessed = :new_date "
                "WHERE ast_node_id = :node_id"
            ),
            {"new_date": new_date, "node_id": node_new.id},
        )
        await session.execute(
            text(
                "UPDATE ast_context_edges SET last_accessed = :old_date "
                "WHERE ast_node_id = :node_id"
            ),
            {"old_date": old_date, "node_id": node_inactive.id},
        )
        await session.commit()

    # Run the staleness cron
    pruned_count = await run_staleness_pruning(engine, cutoff_days=180, active_days=30)
    assert pruned_count == 1  # Only node_old (active repo, 190 days old) should be pruned
    assert sorted(c.args[0] for c in mock_client_class.call_args_list) == [
        "tok_active",
        "tok_inactive",
    ]

    # Assert database states
    async with async_session() as session:
        # Check active repo old edge -> stale
        res_old = await session.execute(
            select(ASTContextEdge).where(ASTContextEdge.ast_node_id == node_old.id)
        )
        assert res_old.scalar_one().status.value == "stale"

        # Check active repo new edge -> remains active
        res_new = await session.execute(
            select(ASTContextEdge).where(ASTContextEdge.ast_node_id == node_new.id)
        )
        assert res_new.scalar_one().status.value == "active"

        # Check inactive repo old edge -> remains active (due to repo activity guardrail!)
        res_inactive = await session.execute(
            select(ASTContextEdge).where(ASTContextEdge.ast_node_id == node_inactive.id)
        )
        assert res_inactive.scalar_one().status.value == "active"

        # 3. Verify touch verification: check that querying via find_ast_nodes_by_paths
        # updates last_accessed
        # Initialize and update one of the active edges
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            initial_edge_res = await session.execute(
                select(ASTContextEdge.last_accessed).where(
                    ASTContextEdge.ast_node_id == node_new.id
                )
            )
            initial_time = initial_edge_res.scalar_one()

            # Query the path to trigger "find_ast_nodes_by_paths"
            await store.find_ast_nodes_by_paths(
                tenant_id=tenant_id, repo_identifier=repo_active, file_paths=["src/new.py"]
            )

        # Retrieve new timestamp
        updated_edge_res = await session.execute(
            select(ASTContextEdge.last_accessed).where(ASTContextEdge.ast_node_id == node_new.id)
        )
        updated_time = updated_edge_res.scalar_one()
        assert updated_time > initial_time  # Must have been updated/touched
