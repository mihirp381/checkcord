from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
import yaml
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from checkcord.core.config import get_settings
from checkcord.services.context_formatter import ContextFormatterService
from checkcord.services.repo_audience import RepoAudience
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
async def test_staleness_skips_locked_contexts(
    mock_client_class: Any, db_session_and_engine: Any
) -> None:
    """Verify that run_staleness_pruning skips edges when the canonical context is locked."""
    async_session, engine = db_session_and_engine

    mock_client = AsyncMock()
    mock_client_class.return_value = mock_client
    mock_client.has_recent_commits.return_value = True

    tenant_id = uuid4()
    repo_active = "my-org/repo-active"

    async with async_session() as session:
        # Create GitHub installation
        inst = GithubInstallation(
            id=uuid4(),
            repo_identifier=repo_active,
            tenant_id=tenant_id,
            personal_access_token="tok",
            webhook_secret="sec",
        )
        session.add(inst)

        # Create 2 Canonical Contexts: one locked, one unlocked
        ctx_locked = CanonicalContext(
            id=uuid4(),
            tenant_id=tenant_id,
            summary="Rule 1: Always encrypt - LOCKED",
            is_locked=True,
        )
        ctx_unlocked = CanonicalContext(
            id=uuid4(),
            tenant_id=tenant_id,
            summary="Rule 2: Avoid direct DB connections - UNLOCKED",
            is_locked=False,
        )
        session.add_all([ctx_locked, ctx_unlocked])

        # Create AST Nodes
        node_locked = ASTNode(
            id=uuid4(),
            tenant_id=tenant_id,
            repo_identifier=repo_active,
            file_path="src/locked.py",
            symbol_name="encrypt_data",
            node_type="function",
            merkle_hash="hash_locked",
        )
        node_unlocked = ASTNode(
            id=uuid4(),
            tenant_id=tenant_id,
            repo_identifier=repo_active,
            file_path="src/unlocked.py",
            symbol_name="query_db",
            node_type="function",
            merkle_hash="hash_unlocked",
        )
        session.add_all([node_locked, node_unlocked])
        await session.commit()

        # Link them
        edge_locked = ASTContextEdge(
            ast_node_id=node_locked.id,
            canonical_id=ctx_locked.id,
            tenant_id=tenant_id,
            status="active",
        )
        edge_unlocked = ASTContextEdge(
            ast_node_id=node_unlocked.id,
            canonical_id=ctx_unlocked.id,
            tenant_id=tenant_id,
            status="active",
        )
        session.add_all([edge_locked, edge_unlocked])
        await session.commit()

        # Set last_accessed to 190 days ago for both
        old_date = datetime.now(UTC) - timedelta(days=190)
        await session.execute(
            text(
                "UPDATE ast_context_edges SET last_accessed = :old_date "
                "WHERE ast_node_id = :node_id"
            ),
            {"old_date": old_date, "node_id": node_locked.id},
        )
        await session.execute(
            text(
                "UPDATE ast_context_edges SET last_accessed = :old_date "
                "WHERE ast_node_id = :node_id"
            ),
            {"old_date": old_date, "node_id": node_unlocked.id},
        )
        await session.commit()

    # Run the staleness cron
    pruned_count = await run_staleness_pruning(engine, cutoff_days=180, active_days=30)
    assert pruned_count == 1  # Only the unlocked one should be marked stale

    # Assert database states
    async with async_session() as session:
        # Locked context edge remains active
        res_locked = await session.execute(
            select(ASTContextEdge).where(ASTContextEdge.ast_node_id == node_locked.id)
        )
        assert res_locked.scalar_one().status.value == "active"

        # Unlocked context edge becomes stale
        res_unlocked = await session.execute(
            select(ASTContextEdge).where(ASTContextEdge.ast_node_id == node_unlocked.id)
        )
        assert res_unlocked.scalar_one().status.value == "stale"


async def test_context_formatter_writes_locked_frontmatter(db_session_and_engine: Any) -> None:
    """Verify context formatter renders `locked: true` and parser syncs it back."""
    async_session, engine = db_session_and_engine

    tenant_id = uuid4()
    repo_name = "my-org/my-repo"

    async with async_session() as session:
        # Create GitHub installation
        inst = GithubInstallation(
            id=uuid4(),
            repo_identifier=repo_name,
            tenant_id=tenant_id,
            personal_access_token="tok",
            webhook_secret="sec",
        )
        session.add(inst)

        # Create context
        ctx = CanonicalContext(
            id=uuid4(),
            tenant_id=tenant_id,
            summary="System design rule",
            context_type="adr",
            is_locked=True,
            access_control={"is_public": True},
        )
        session.add(ctx)

        node = ASTNode(
            id=uuid4(),
            tenant_id=tenant_id,
            repo_identifier=repo_name,
            file_path="src/app.py",
            symbol_name="run_app",
            node_type="function",
            merkle_hash="hash_app",
        )
        session.add(node)
        await session.commit()

        edge = ASTContextEdge(
            ast_node_id=node.id, canonical_id=ctx.id, tenant_id=tenant_id, status="active"
        )
        session.add(edge)
        await session.commit()

    async with tenant_context(engine, tenant_id) as conn:
        store = PostgresEventStore(conn)

        # Format/compile to ARCHITECTURE.md
        formatter = ContextFormatterService()
        compiled = await formatter.compile_for_repo(tenant_id, repo_name, store, RepoAudience())
        arch_md = compiled.architecture_md

        front = yaml.safe_load(arch_md.split("---\n")[1])
        assert [(c["id"], c["locked"]) for c in front["contexts"]] == [(str(ctx.id), True)]
