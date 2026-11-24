from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from checkcord.core.config import get_settings
from checkcord.models.enums import ASTEdgeStatus
from checkcord.storage.models import ASTContextEdge, ASTNode, CanonicalContext
from checkcord.storage.postgres import PostgresEventStore, tenant_context

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def setup_rls_role(engine: AsyncEngine) -> None:
    """Helper to set up the non-superuser RLS test role."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "DO $$\n"
                "BEGIN\n"
                "    IF NOT EXISTS (\n"
                "        SELECT FROM pg_catalog.pg_roles WHERE rolname = 'rls_test_user'\n"
                "    ) THEN\n"
                "        CREATE ROLE rls_test_user WITH LOGIN;\n"
                "    END IF;\n"
                "END\n"
                "$$;"
            )
        )
        await conn.execute(text("GRANT USAGE ON SCHEMA public TO rls_test_user"))
        await conn.execute(
            text("GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO rls_test_user")
        )
        await conn.execute(
            text("GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO rls_test_user")
        )


async def test_ast_nodes_crud_and_cascades() -> None:
    """Verify basic CRUD, constraints, and cascade delete behavior for AST models."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    tenant_id = uuid4()
    context_id = uuid4()
    node_id = uuid4()

    async with async_session() as session:
        # 1. Insert Canonical Context
        ctx = CanonicalContext(
            id=context_id,
            tenant_id=tenant_id,
            summary="A test architectural context",
            confidence_score=0.95,
        )
        session.add(ctx)

        # 2. Insert AST Node
        node = ASTNode(
            id=node_id,
            tenant_id=tenant_id,
            repo_identifier="github.com/org/repo",
            file_path="src/main.py",
            symbol_name="main_func",
            node_type="function",
            merkle_hash="abc123hash",
        )
        session.add(node)
        await session.commit()

    async with async_session() as session:
        # 3. Create Context Edge link
        edge = ASTContextEdge(
            ast_node_id=node_id,
            canonical_id=context_id,
            tenant_id=tenant_id,
            status=ASTEdgeStatus.ACTIVE,
        )
        session.add(edge)
        await session.commit()

    async with async_session() as session:
        # 4. Verify links exist
        stmt = select(ASTContextEdge).where(
            ASTContextEdge.ast_node_id == node_id, ASTContextEdge.canonical_id == context_id
        )
        res = await session.execute(stmt)
        edge_db = res.scalar_one_or_none()
        assert edge_db is not None
        assert edge_db.status == ASTEdgeStatus.ACTIVE

        # 5. Delete AST Node and verify cascade deletion of the link
        res_node = await session.execute(select(ASTNode).where(ASTNode.id == node_id))
        node_db = res_node.scalar_one()
        await session.delete(node_db)
        await session.commit()

    async with async_session() as session:
        # Check edge cascaded
        stmt = select(ASTContextEdge).where(ASTContextEdge.ast_node_id == node_id)
        res = await session.execute(stmt)
        assert res.scalar_one_or_none() is None

        # Clean up Canonical Context
        res_ctx = await session.execute(
            select(CanonicalContext).where(CanonicalContext.id == context_id)
        )
        ctx_db = res_ctx.scalar_one()
        await session.delete(ctx_db)
        await session.commit()

    await engine.dispose()


async def test_ast_nodes_unique_constraint() -> None:
    """Verify unique constraint on (tenant, repo, path, symbol, type)."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    tenant_id = uuid4()
    node_a_id = uuid4()
    node_b_id = uuid4()

    async with async_session() as session:
        node_a = ASTNode(
            id=node_a_id,
            tenant_id=tenant_id,
            repo_identifier="github.com/org/repo",
            file_path="src/main.py",
            symbol_name="duplicate_symbol",
            node_type="class",
            merkle_hash="hash_a",
        )
        session.add(node_a)
        await session.commit()

    async with async_session() as session:
        # Duplicate repo/path/symbol/type under same tenant
        node_b = ASTNode(
            id=node_b_id,
            tenant_id=tenant_id,
            repo_identifier="github.com/org/repo",
            file_path="src/main.py",
            symbol_name="duplicate_symbol",
            node_type="class",
            merkle_hash="hash_b",
        )
        session.add(node_b)
        with pytest.raises(IntegrityError):
            await session.commit()

    # Clean up node_a
    async with async_session() as session:
        res = await session.execute(select(ASTNode).where(ASTNode.id == node_a_id))
        node_a_db = res.scalar_one_or_none()
        if node_a_db:
            await session.delete(node_a_db)
            await session.commit()

    await engine.dispose()


async def test_ast_nodes_rls_isolation() -> None:
    """Verify that Row-Level Security policies isolate tenant data for AST tables."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    await setup_rls_role(engine)

    tenant_a = uuid4()
    tenant_b = uuid4()

    node_a_id = uuid4()
    node_b_id = uuid4()
    context_a_id = uuid4()

    # 1. Insert test records as Superuser (RLS bypassed)
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as session:
        ctx_a = CanonicalContext(
            id=context_a_id,
            tenant_id=tenant_a,
            summary="Tenant A Context",
        )
        session.add(ctx_a)

        node_a = ASTNode(
            id=node_a_id,
            tenant_id=tenant_a,
            repo_identifier="github.com/org/repo",
            file_path="src/main.py",
            symbol_name="sym_a",
            node_type="method",
            merkle_hash="hash_a",
        )
        node_b = ASTNode(
            id=node_b_id,
            tenant_id=tenant_b,
            repo_identifier="github.com/org/repo",
            file_path="src/main.py",
            symbol_name="sym_b",
            node_type="method",
            merkle_hash="hash_b",
        )
        session.add(node_a)
        session.add(node_b)
        await session.commit()

    async with async_session() as session:
        edge_a = ASTContextEdge(
            ast_node_id=node_a_id,
            canonical_id=context_a_id,
            tenant_id=tenant_a,
            status=ASTEdgeStatus.ACTIVE,
        )
        session.add(edge_a)
        await session.commit()

    # 2. Test RLS as tenant_a via rls_test_user
    async with tenant_context(engine, tenant_a) as conn_a:
        await conn_a.execute(text("SET LOCAL ROLE rls_test_user"))

        # Can read own ASTNode
        res = await conn_a.execute(
            text("SELECT id, symbol_name FROM ast_nodes WHERE id = :id"), {"id": node_a_id}
        )
        rows = res.fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "sym_a"

        # Cannot read tenant_b's ASTNode
        res = await conn_a.execute(
            text("SELECT id FROM ast_nodes WHERE id = :id"), {"id": node_b_id}
        )
        assert len(res.fetchall()) == 0

        # Can read own ASTContextEdge
        res = await conn_a.execute(
            text("SELECT status FROM ast_context_edges WHERE ast_node_id = :node_id"),
            {"node_id": node_a_id},
        )
        rows = res.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "active"

    # 3. Test RLS as tenant_b via rls_test_user
    async with tenant_context(engine, tenant_b) as conn_b:
        await conn_b.execute(text("SET LOCAL ROLE rls_test_user"))

        # Cannot read tenant_a's ASTNode
        res = await conn_b.execute(
            text("SELECT id FROM ast_nodes WHERE id = :id"), {"id": node_a_id}
        )
        assert len(res.fetchall()) == 0

        # Cannot read tenant_a's ASTContextEdge
        res = await conn_b.execute(
            text("SELECT status FROM ast_context_edges WHERE ast_node_id = :node_id"),
            {"node_id": node_a_id},
        )
        assert len(res.fetchall()) == 0

    # 4. Clean up as superuser
    async with async_session() as session:
        # Delete links and nodes
        res_edge = await session.execute(
            select(ASTContextEdge).where(ASTContextEdge.ast_node_id == node_a_id)
        )
        edge = res_edge.scalar_one_or_none()
        if edge:
            await session.delete(edge)

        res_node_a = await session.execute(select(ASTNode).where(ASTNode.id == node_a_id))
        node_a_db = res_node_a.scalar_one_or_none()
        if node_a_db:
            await session.delete(node_a_db)

        res_node_b = await session.execute(select(ASTNode).where(ASTNode.id == node_b_id))
        node_b_db = res_node_b.scalar_one_or_none()
        if node_b_db:
            await session.delete(node_b_db)

        res_ctx = await session.execute(
            select(CanonicalContext).where(CanonicalContext.id == context_a_id)
        )
        ctx_db = res_ctx.scalar_one_or_none()
        if ctx_db:
            await session.delete(ctx_db)

        await session.commit()

    await engine.dispose()


async def test_upsert_ast_node_and_check_drift() -> None:
    """Verify insert/no-drift/drift-detected transitions and tenant isolation.

    Exercises PostgresEventStore.upsert_ast_node_and_check_drift, the synchronous
    drift-detection path called from api/graph_router.py.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    await setup_rls_role(engine)

    tenant_a = uuid4()
    tenant_b = uuid4()
    context_id = uuid4()

    try:
        # Seed Context under Tenant A
        async with tenant_context(engine, tenant_a) as conn_a:
            await conn_a.execute(text("SET LOCAL ROLE rls_test_user"))
            store_a = PostgresEventStore(conn_a)

            await store_a.save_canonical_context(
                tenant_id=tenant_a,
                summary="Some context mentioning drift",
                confidence_score=0.9,
                id=context_id,
            )

            # Test 1: Node doesn't exist yet, should insert and return False
            drift_detected = await store_a.upsert_ast_node_and_check_drift(
                tenant_id=tenant_a,
                repo_identifier="github.com/org/repo",
                file_path="src/main.py",
                symbol_name="main_func",
                node_type="function",
                merkle_hash="hash1",
            )
            assert drift_detected is False

            # Query node
            nodes = await store_a.find_ast_nodes_by_symbols(tenant_a, ["main_func"])
            assert len(nodes) == 1
            node_id = nodes[0]["id"]
            assert nodes[0]["merkle_hash"] == "hash1"

            # Create link (edge) to Context
            linked = await store_a.link_ast_to_context(
                tenant_id=tenant_a,
                ast_node_id=node_id,
                canonical_id=context_id,
            )
            assert linked is True

            # Test 2: Node exists and hash is identical, should return False, edge active
            drift_detected = await store_a.upsert_ast_node_and_check_drift(
                tenant_id=tenant_a,
                repo_identifier="github.com/org/repo",
                file_path="src/main.py",
                symbol_name="main_func",
                node_type="function",
                merkle_hash="hash1",
            )
            assert drift_detected is False

            # Verify edge is active
            stmt = select(ASTContextEdge.__table__.c.status).where(
                ASTContextEdge.__table__.c.ast_node_id == node_id,
                ASTContextEdge.__table__.c.canonical_id == context_id,
            )
            res = await conn_a.execute(stmt)
            edge_row = res.mappings().fetchone()
            assert edge_row is not None
            assert edge_row["status"] == ASTEdgeStatus.ACTIVE.value

            # Test 3: Node exists and hash changed, should return True, update hash, edge stale
            drift_detected = await store_a.upsert_ast_node_and_check_drift(
                tenant_id=tenant_a,
                repo_identifier="github.com/org/repo",
                file_path="src/main.py",
                symbol_name="main_func",
                node_type="function",
                merkle_hash="hash2",
            )
            assert drift_detected is True

            # Verify hash updated
            nodes = await store_a.find_ast_nodes_by_symbols(tenant_a, ["main_func"])
            assert nodes[0]["merkle_hash"] == "hash2"

            # Verify edge is now stale
            res = await conn_a.execute(stmt)
            edge_row = res.mappings().fetchone()
            assert edge_row is not None
            assert edge_row["status"] == ASTEdgeStatus.STALE.value

        # Test 4: Tenant isolation
        async with tenant_context(engine, tenant_b) as conn_b:
            await conn_b.execute(text("SET LOCAL ROLE rls_test_user"))
            store_b = PostgresEventStore(conn_b)

            # Under tenant B, main_func should not exist
            nodes = await store_b.find_ast_nodes_by_symbols(tenant_b, ["main_func"])
            assert len(nodes) == 0

    finally:
        # Cleanup
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM ast_context_edges"))
            await conn.execute(text("DELETE FROM ast_nodes"))
            await conn.execute(text("DELETE FROM canonical_contexts"))
        await engine.dispose()
