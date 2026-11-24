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
from checkcord.services.repo_audience import RepoAudience
from checkcord.storage.models import (
    ASTContextEdge,
    ASTNode,
    CanonicalContext,
    GithubInstallation,
)
from checkcord.workers.deprecation_worker import run_deprecation_pr_generation

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

PUBLIC = {"is_public": True, "allowed_identity_ids": [], "admin_overrides": []}


@pytest.fixture(autouse=True)
def audience_lookup(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    lookup = AsyncMock(return_value=RepoAudience())
    monkeypatch.setattr("checkcord.workers.deprecation_worker.resolve_repo_audience", lookup)
    return lookup


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


@patch("checkcord.workers.deprecation_worker.GitHubPRService")
async def test_run_deprecation_pr_generation_success(
    mock_pr_service_class: Any, db_session_and_engine: Any
) -> None:
    """Verify that run_deprecation_pr_generation correctly flags stale edges as deprecated,
    compiles updated files without them, and proposes a PR via GitHubPRService.
    """
    async_session, engine = db_session_and_engine

    mock_pr_service = AsyncMock()
    mock_pr_service_class.return_value = mock_pr_service
    mock_pr_service.propose_context_update.return_value = True

    tenant_id = uuid4()
    repo_name = "my-org/my-repo"

    # 1. Seed installation, canonical context, AST node, and STALE edge
    async with async_session() as session:
        inst = GithubInstallation(
            id=uuid4(),
            repo_identifier=repo_name,
            tenant_id=tenant_id,
            personal_access_token="test_token",
            webhook_secret="test_sec",
        )
        session.add(inst)

        # Stale context/edge
        ctx_stale = CanonicalContext(
            id=uuid4(),
            tenant_id=tenant_id,
            summary="Stale rule to be deprecated",
            access_control=PUBLIC,
        )
        # Active context/edge (remains)
        ctx_active = CanonicalContext(
            id=uuid4(), tenant_id=tenant_id, summary="Active rule to keep", access_control=PUBLIC
        )
        session.add_all([ctx_stale, ctx_active])

        node_stale = ASTNode(
            id=uuid4(),
            tenant_id=tenant_id,
            repo_identifier=repo_name,
            file_path="src/stale_file.py",
            symbol_name="stale_func",
            node_type="function",
            merkle_hash="hash1",
        )
        node_active = ASTNode(
            id=uuid4(),
            tenant_id=tenant_id,
            repo_identifier=repo_name,
            file_path="src/active_file.py",
            symbol_name="active_func",
            node_type="function",
            merkle_hash="hash2",
        )
        session.add_all([node_stale, node_active])
        await session.commit()

        edge_stale = ASTContextEdge(
            ast_node_id=node_stale.id,
            canonical_id=ctx_stale.id,
            tenant_id=tenant_id,
            status="stale",
        )
        edge_active = ASTContextEdge(
            ast_node_id=node_active.id,
            canonical_id=ctx_active.id,
            tenant_id=tenant_id,
            status="active",
        )
        session.add_all([edge_stale, edge_active])
        await session.commit()

    # 2. Run the deprecation PR generator worker
    total_proposed = await run_deprecation_pr_generation(engine)
    assert total_proposed == 1

    # 3. Assert mock call to GitHubPRService
    mock_pr_service.propose_context_update.assert_called_once()
    call_args = mock_pr_service.propose_context_update.call_args[1]
    assert call_args["repo_full_name"] == repo_name
    assert call_args["pat"] == "test_token"
    assert call_args["branch_name"] == "checkcord/deprecate-stale-context"
    assert call_args["pr_title"] == "[chore] Deprecate stale architectural context"
    assert "stale_func" in call_args["pr_body"]
    assert "src/stale_file.py" in call_args["pr_body"]
    assert "Stale rule to be deprecated" in call_args["pr_body"]

    # 4. Assert database status changes
    async with async_session() as session:
        # The stale edge should now be marked deprecated
        res_stale = await session.execute(
            select(ASTContextEdge).where(ASTContextEdge.ast_node_id == node_stale.id)
        )
        assert res_stale.scalar_one().status.value == "deprecated"

        # The active edge remains active
        res_active = await session.execute(
            select(ASTContextEdge).where(ASTContextEdge.ast_node_id == node_active.id)
        )
        assert res_active.scalar_one().status.value == "active"


@patch("checkcord.workers.deprecation_worker.GitHubPRService")
async def test_run_deprecation_pr_generation_failure_does_not_update_db(
    mock_pr_service_class: Any, db_session_and_engine: Any
) -> None:
    """Verify that if PR generation fails (propose_context_update returns False),
    the database edge status remains 'stale' instead of transitioning to 'deprecated'.
    """
    async_session, engine = db_session_and_engine

    mock_pr_service = AsyncMock()
    mock_pr_service_class.return_value = mock_pr_service
    mock_pr_service.propose_context_update.return_value = False

    tenant_id = uuid4()
    repo_name = "my-org/my-repo"

    # 1. Seed installation, ASTNode, and STALE edge
    async with async_session() as session:
        inst = GithubInstallation(
            id=uuid4(),
            repo_identifier=repo_name,
            tenant_id=tenant_id,
            personal_access_token="test_token",
            webhook_secret="test_sec",
        )
        session.add(inst)

        ctx = CanonicalContext(
            id=uuid4(), tenant_id=tenant_id, summary="Stale rule to be deprecated"
        )
        session.add(ctx)

        node = ASTNode(
            id=uuid4(),
            tenant_id=tenant_id,
            repo_identifier=repo_name,
            file_path="src/stale_file.py",
            symbol_name="stale_func",
            node_type="function",
            merkle_hash="hash1",
        )
        session.add(node)
        await session.commit()

        edge = ASTContextEdge(
            ast_node_id=node.id, canonical_id=ctx.id, tenant_id=tenant_id, status="stale"
        )
        session.add(edge)
        await session.commit()

    # 2. Run the deprecation PR generator worker
    total_proposed = await run_deprecation_pr_generation(engine)
    assert total_proposed == 0

    # 3. Assert database status remains 'stale'
    async with async_session() as session:
        res = await session.execute(
            select(ASTContextEdge).where(ASTContextEdge.ast_node_id == node.id)
        )
        assert res.scalar_one().status.value == "stale"


@patch("checkcord.workers.deprecation_worker.GitHubPRService")
async def test_run_deprecation_skips_repo_without_stored_token(
    mock_pr_service_class: Any, db_session_and_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async_session, engine = db_session_and_engine
    lookup = AsyncMock(side_effect=LookupError("no stored GitHub token for my-org/my-repo"))
    monkeypatch.setattr("checkcord.workers.deprecation_worker.github_token_for", lookup)
    tenant_id = uuid4()
    async with async_session() as session:
        session.add(
            GithubInstallation(
                id=uuid4(),
                repo_identifier="my-org/my-repo",
                tenant_id=tenant_id,
                personal_access_token="",
                webhook_secret="test_sec",
            )
        )
        await session.commit()

    assert await run_deprecation_pr_generation(engine) == 0
    lookup.assert_awaited_once_with(engine, tenant_id, "my-org/my-repo")
    mock_pr_service_class.return_value.propose_context_update.assert_not_called()


@patch("checkcord.workers.deprecation_worker.GitHubPRService")
async def test_deprecation_pr_leaves_out_records_the_repository_may_not_see(
    mock_pr_service_class: Any, db_session_and_engine: Any, audience_lookup: AsyncMock
) -> None:
    async_session, engine = db_session_and_engine
    mock_pr_service = mock_pr_service_class.return_value
    mock_pr_service.propose_context_update = AsyncMock(return_value=True)
    tenant_id, member = uuid4(), uuid4()
    repo_name = "my-org/my-repo"
    audience_lookup.return_value = RepoAudience(frozenset({str(member)}))
    token = "ghp_" + "A1b2C3d4" * 4
    records = {
        "shared": {"is_public": False, "allowed_identity_ids": [str(member)]},
        "restricted": {"is_public": False, "allowed_identity_ids": []},
    }

    async with async_session() as session:
        session.add(
            GithubInstallation(
                id=uuid4(),
                repo_identifier=repo_name,
                tenant_id=tenant_id,
                personal_access_token="test_token",
                webhook_secret="test_sec",
            )
        )
        nodes = {}
        for name, access in records.items():
            ctx = CanonicalContext(
                id=uuid4(),
                tenant_id=tenant_id,
                summary=f"{name.upper()}-RULE mail ops@acme.example with {token}",
                access_control=access,
            )
            node = ASTNode(
                id=uuid4(),
                tenant_id=tenant_id,
                repo_identifier=repo_name,
                file_path=f"src/{name}.py",
                symbol_name=f"{name}_func",
                node_type="function",
                merkle_hash="hash",
            )
            session.add_all([ctx, node])
            nodes[name] = (node.id, ctx.id)
        await session.commit()
        for node_id, ctx_id in nodes.values():
            session.add(
                ASTContextEdge(
                    ast_node_id=node_id, canonical_id=ctx_id, tenant_id=tenant_id, status="stale"
                )
            )
        await session.commit()

    assert await run_deprecation_pr_generation(engine) == 1

    pr_body = mock_pr_service.propose_context_update.call_args[1]["pr_body"]
    assert "SHARED-RULE" in pr_body
    assert "shared_func" in pr_body
    assert "RESTRICTED-RULE" not in pr_body
    assert "restricted_func" not in pr_body
    assert str(nodes["restricted"][1]) not in pr_body
    assert "ops@acme.example" not in pr_body
    assert token not in pr_body
    assert "[REDACTED:EMAIL]" in pr_body
    assert "[REDACTED:TOKEN]" in pr_body

    async with async_session() as session:
        res = await session.execute(
            select(ASTContextEdge.status).where(ASTContextEdge.tenant_id == tenant_id)
        )
        assert {status.value for status in res.scalars()} == {"deprecated"}
