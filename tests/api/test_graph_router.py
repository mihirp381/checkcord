import hashlib
from collections.abc import AsyncGenerator, Iterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from key_helpers import KeySpec, seeded_keys, tenant_keys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.core.agent_text import NOTICE
from checkcord.core.config import get_settings
from checkcord.main import app
from checkcord.storage.postgres import PostgresEventStore, tenant_context

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _repo_scopes(rows: list[dict[str, str]]) -> list[str]:
    return [r["scope_value"] for r in rows if r["scope_type"] == "repo"]


@pytest.fixture(scope="module", autouse=True)
def setup_tokens() -> Iterator[dict[str, str]]:
    """Setup settings mapping bearer tokens to tenant UUIDs for graph tests."""
    tenant_a = str(uuid4())
    tenant_b = str(uuid4())
    token_a = "token_graph_a"
    token_b = "token_graph_b"
    tokens = {
        token_a: tenant_a,
        token_b: tenant_b,
    }
    with tenant_keys(tokens):
        yield tokens


_ADMIN_USERS = ("alice@acme.example", "bob@acme.example")


def _admin_token(email: str) -> str:
    return f"token_graph_admin_{email.split('@')[0]}"


def _admin_as(email: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_admin_token(email)}"}


def _admin_b() -> dict[str, str]:
    return {"Authorization": "Bearer token_graph_admin_b"}


@pytest_asyncio.fixture(autouse=True)
async def graph_admin_keys(setup_tokens: dict[str, str]) -> AsyncGenerator[None, None]:
    tenant_a, tenant_b = UUID(setup_tokens["token_graph_a"]), UUID(setup_tokens["token_graph_b"])
    specs = [KeySpec(_admin_token(e), tenant_a, admin=True, user_email=e) for e in _ADMIN_USERS]
    specs += [
        KeySpec("token_graph_admin_b", tenant_b, admin=True),
        KeySpec("token_graph_admin_unbound", tenant_a, admin=True),
    ]
    async with seeded_keys(specs):
        yield


@pytest_asyncio.fixture
async def cleanup_db(setup_tokens: dict[str, str]) -> AsyncGenerator[None, None]:
    """Clean up DB after test run."""
    yield
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(text("DELETE FROM ast_context_edges"))
        await conn.execute(text("DELETE FROM ast_nodes"))
        await conn.execute(text("DELETE FROM canonical_contexts"))
    await engine.dispose()


async def test_authentication_graph() -> None:
    """Verify that unauthorized requests to graph API return 401."""
    with TestClient(app) as client:
        # GET without auth
        for path in ("/api/v1/graph/context", "/api/v1/graph/impact"):
            res = client.get(f"{path}?symbol=my_symbol")
            assert res.status_code == 401

            # GET with invalid token
            res_bad = client.get(
                f"{path}?symbol=my_symbol",
                headers={"Authorization": "Bearer bad_token"},
            )
            assert res_bad.status_code == 401


async def test_context_lookup_logic(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """Verify retrieving canonical contexts linked to active AST nodes and filtering stale ones."""
    tenant_a = UUID(setup_tokens["token_graph_a"])

    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    # Ensure rls_test_user exists
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

    # 1. Seed database under Tenant A
    async with tenant_context(engine, tenant_a) as conn_a:
        await conn_a.execute(text("SET LOCAL ROLE rls_test_user"))
        store_a = PostgresEventStore(conn_a)

        # Contexts
        ctx1_id = uuid4()
        ctx2_id = uuid4()
        ctx3_id = uuid4()

        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="This is context 1 for calculate_total",
            confidence_score=9.5,
            id=ctx1_id,
        )
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="This is context 2 for calculate_total (lower confidence)",
            confidence_score=4.2,
            id=ctx2_id,
        )
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="This is stale context for calculate_total",
            confidence_score=8.0,
            id=ctx3_id,
        )

        # AST nodes
        await store_a.upsert_ast_node_and_check_drift(
            tenant_id=tenant_a,
            repo_identifier="github.com/org/repo",
            file_path="src/calc.py",
            symbol_name="calculate_total",
            node_type="function",
            merkle_hash="hash123",
        )
        nodes = await store_a.find_ast_nodes_by_symbols(tenant_a, ["calculate_total"])
        node_id = nodes[0]["id"]

        # Link contexts to the AST node
        await store_a.link_ast_to_context(tenant_a, node_id, ctx1_id)
        await store_a.link_ast_to_context(tenant_a, node_id, ctx2_id)
        await store_a.link_ast_to_context(tenant_a, node_id, ctx3_id)

        # Force context 3 edge to be STALE
        await conn_a.execute(
            text(
                "UPDATE ast_context_edges SET status = 'stale' "
                "WHERE canonical_id = :cid AND ast_node_id = :nid"
            ),
            {"cid": ctx3_id, "nid": node_id},
        )

        # Upsert a second node with same symbol name but different file path
        ctx4_id = uuid4()
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="This is context for calculate_total in tests",
            confidence_score=5.0,
            id=ctx4_id,
        )
        await store_a.upsert_ast_node_and_check_drift(
            tenant_id=tenant_a,
            repo_identifier="github.com/org/repo",
            file_path="tests/test_calc.py",
            symbol_name="calculate_total",
            node_type="function",
            merkle_hash="hash999",
        )
        nodes_calc = await store_a.find_ast_nodes_by_symbols(tenant_a, ["calculate_total"])
        node2_id = [n["id"] for n in nodes_calc if n["file_path"] == "tests/test_calc.py"][0]
        await store_a.link_ast_to_context(tenant_a, node2_id, ctx4_id)

    # Clean up engine connection before using client
    await engine.dispose()

    # 2. Query contexts via HTTP Client
    token_a = "token_graph_a"
    headers_a = {"Authorization": f"Bearer {token_a}"}

    with TestClient(app) as client:
        # A. Query calculate_total without file_path filter
        # Should return contexts 1, 2, and 4 (stale 3 is filtered, ordered by confidence DESC)
        res = client.get(
            "/api/v1/graph/context?symbol=calculate_total",
            headers=headers_a,
        )
        assert res.status_code == 200
        data = res.json()["contexts"]
        assert len(data) == 3
        # Ordered by confidence DESC: 9.5 (ctx1), 5.0 (ctx4), 4.2 (ctx2)
        assert data[0]["summary"] == "This is context 1 for calculate_total"
        assert data[0]["confidence_score"] == 9.5
        assert data[1]["summary"] == "This is context for calculate_total in tests"
        assert data[1]["confidence_score"] == 5.0
        assert data[2]["summary"] == "This is context 2 for calculate_total (lower confidence)"
        assert data[2]["confidence_score"] == 4.2

        # B. Query calculate_total with file_path filter "src/calc.py"
        # Should only return contexts 1 and 2 (filtering out ctx4 from tests/test_calc.py)
        res_filtered = client.get(
            "/api/v1/graph/context?symbol=calculate_total&file_path=src/calc.py",
            headers=headers_a,
        )
        assert res_filtered.status_code == 200
        data_filtered = res_filtered.json()["contexts"]
        assert len(data_filtered) == 2
        assert data_filtered[0]["summary"] == "This is context 1 for calculate_total"
        assert data_filtered[1]["summary"] == (
            "This is context 2 for calculate_total (lower confidence)"
        )

        # B2. Query calculate_total with absolute file_path filter
        # "/Users/alex/workspace/repo/src/calc.py"
        # Should verify suffix matching in get_context_for_symbol works and returns contexts 1 and 2
        res_abs_filtered = client.get(
            "/api/v1/graph/context?symbol=calculate_total&file_path=/Users/alex/workspace/repo/src/calc.py",
            headers=headers_a,
        )
        assert res_abs_filtered.status_code == 200
        data_abs_filtered = res_abs_filtered.json()["contexts"]
        assert len(data_abs_filtered) == 2
        assert data_abs_filtered[0]["summary"] == "This is context 1 for calculate_total"
        assert data_abs_filtered[1]["summary"] == (
            "This is context 2 for calculate_total (lower confidence)"
        )

        # C. Query symbol that doesn't exist
        res_empty = client.get(
            "/api/v1/graph/context?symbol=non_existent",
            headers=headers_a,
        )
        assert res_empty.status_code == 200
        assert res_empty.json()["contexts"] == []
        assert res_empty.json()["notice"].startswith("Record text is quoted")

        # D. Tenant isolation check: Tenant B querying calculate_total should return []
        token_b = "token_graph_b"
        headers_b = {"Authorization": f"Bearer {token_b}"}
        res_tenant_b = client.get(
            "/api/v1/graph/context?symbol=calculate_total",
            headers=headers_b,
        )
        assert res_tenant_b.status_code == 200
        assert res_tenant_b.json()["contexts"] == []


async def test_blast_radius_impact_logic(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """Verify retrieving canonical contexts and generating a formatted warning header."""
    tenant_a = UUID(setup_tokens["token_graph_a"])

    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    # 1. Seed database under Tenant A
    async with tenant_context(engine, tenant_a) as conn_a:
        await conn_a.execute(text("SET LOCAL ROLE rls_test_user"))
        store_a = PostgresEventStore(conn_a)

        # Contexts
        ctx1_id = uuid4()
        ctx2_id = uuid4()
        ctx3_id = uuid4()

        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="Do not modify calculation order due to audit requirements",
            confidence_score=9.5,
            id=ctx1_id,
        )
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="Ensure calculations are rounded to two decimals",
            confidence_score=4.2,
            id=ctx2_id,
        )
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="Old constraint (stale)",
            confidence_score=8.0,
            id=ctx3_id,
        )

        # AST nodes
        await store_a.upsert_ast_node_and_check_drift(
            tenant_id=tenant_a,
            repo_identifier="github.com/org/repo",
            file_path="src/calc.py",
            symbol_name="audit_calc",
            node_type="function",
            merkle_hash="hash123",
        )
        nodes = await store_a.find_ast_nodes_by_symbols(tenant_a, ["audit_calc"])
        node_id = nodes[0]["id"]

        # Link contexts to the AST node
        await store_a.link_ast_to_context(tenant_a, node_id, ctx1_id)
        await store_a.link_ast_to_context(tenant_a, node_id, ctx2_id)
        await store_a.link_ast_to_context(tenant_a, node_id, ctx3_id)

        # Force context 3 edge to be STALE
        await conn_a.execute(
            text(
                "UPDATE ast_context_edges SET status = 'stale' "
                "WHERE canonical_id = :cid AND ast_node_id = :nid"
            ),
            {"cid": ctx3_id, "nid": node_id},
        )

    # Clean up engine connection before using client
    await engine.dispose()

    # 2. Query impact via HTTP Client
    token_a = "token_graph_a"
    headers_a = {"Authorization": f"Bearer {token_a}"}

    with TestClient(app) as client:
        # A. Query audit_calc
        # Should return warning header with contexts 1 and 2 (ordered by confidence DESC)
        # Context 3 is stale, so it should be filtered out
        res = client.get(
            "/api/v1/graph/impact?symbol=audit_calc",
            headers=headers_a,
        )
        assert res.status_code == 200
        data = res.json()
        assert data["symbol"] == "audit_calc"
        warning = data["warning_header"]

        assert "### WARNING: Active Design Constraints for symbol 'audit_calc'" in warning
        assert "The following high-confidence constraints or contexts were found:" in warning
        expected_ctx1 = (
            "- **[Confidence: 9.5]** Do not modify calculation order due to audit requirements"
        )
        assert expected_ctx1 in warning
        expected_ctx2 = "- **[Confidence: 4.2]** Ensure calculations are rounded to two decimals"
        assert expected_ctx2 in warning
        assert "Old constraint" not in warning  # stale

        # B. Query symbol with no contexts linked
        res_empty = client.get(
            "/api/v1/graph/impact?symbol=no_contexts_here",
            headers=headers_a,
        )
        assert res_empty.status_code == 200
        assert res_empty.json() == {
            "symbol": "no_contexts_here",
            "warning_header": "",
            "notice": NOTICE,
        }

        # C. Tenant isolation check: Tenant B querying audit_calc should get an empty warning header
        token_b = "token_graph_b"
        headers_b = {"Authorization": f"Bearer {token_b}"}
        res_tenant_b = client.get(
            "/api/v1/graph/impact?symbol=audit_calc",
            headers=headers_b,
        )
        assert res_tenant_b.status_code == 200
        assert res_tenant_b.json() == {
            "symbol": "audit_calc",
            "warning_header": "",
            "notice": NOTICE,
        }


async def test_graph_active_decisions_logic(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """Verify retrieving active global architectural decisions for a repository."""
    tenant_a = UUID(setup_tokens["token_graph_a"])

    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    # 1. Seed database under Tenant A
    async with tenant_context(engine, tenant_a) as conn_a:
        await conn_a.execute(text("SET LOCAL ROLE rls_test_user"))
        store_a = PostgresEventStore(conn_a)

        # Contexts
        ctx1_id = uuid4()
        ctx2_id = uuid4()

        repo_id = "github.com/company/checkcord"

        # Scoped to repo_id: these are repo-local decisions, so the
        # "unknown repo" query below excludes them.
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="Use asyncio for all database queries",
            confidence_score=9.8,
            id=ctx1_id,
            scope_repo_identifier=repo_id,
            context_type="adr",
        )
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="Avoid direct string concatenation for SQL",
            confidence_score=8.5,
            id=ctx2_id,
            scope_repo_identifier=repo_id,
            context_type="adr",
        )

        # Link ctx1 to multiple nodes in the same repo to test deduplication
        await store_a.upsert_ast_node_and_check_drift(
            tenant_id=tenant_a,
            repo_identifier=repo_id,
            file_path="src/a.py",
            symbol_name="query_a",
            node_type="function",
            merkle_hash="hash_a",
        )
        await store_a.upsert_ast_node_and_check_drift(
            tenant_id=tenant_a,
            repo_identifier=repo_id,
            file_path="src/b.py",
            symbol_name="query_b",
            node_type="function",
            merkle_hash="hash_b",
        )

        nodes = await store_a.find_ast_nodes_by_symbols(tenant_a, ["query_a", "query_b"])
        node1_id = next(n["id"] for n in nodes if n["symbol_name"] == "query_a")
        node2_id = next(n["id"] for n in nodes if n["symbol_name"] == "query_b")

        # Active edge for node 1 -> ctx1
        await store_a.link_ast_to_context(tenant_a, node1_id, ctx1_id)
        # Active edge for node 2 -> ctx1
        await store_a.link_ast_to_context(tenant_a, node2_id, ctx1_id)

        # Link ctx2 to node 1
        await store_a.link_ast_to_context(tenant_a, node1_id, ctx2_id)

    # 2. Test the endpoint via HTTP client
    from fastapi.testclient import TestClient

    from checkcord.main import app

    with TestClient(app) as client:
        token_a = "token_graph_a"
        headers_a = {"Authorization": f"Bearer {token_a}"}

        # A. Query repo
        res = client.get(
            f"/api/v1/graph/decisions/active?repo_identifier={repo_id}",
            headers=headers_a,
        )
        assert res.status_code == 200
        data = res.json()
        assert data["repo_identifier"] == repo_id
        md_content = data["markdown_content"]

        # Deduplication check: it should appear once!
        assert md_content.count("Use asyncio for all database queries") == 1
        assert md_content.count("Avoid direct string concatenation for SQL") == 1

        assert "## [Confidence: 9.8]" in md_content
        assert "## [Confidence: 8.5]" in md_content

        # B. Query unknown repo
        res_empty = client.get(
            "/api/v1/graph/decisions/active?repo_identifier=unknown",
            headers=headers_a,
        )
        assert res_empty.status_code == 200
        data_empty = res_empty.json()
        assert data_empty["markdown_content"] == ""

        # C. Tenant isolation check
        token_b = "token_graph_b"
        headers_b = {"Authorization": f"Bearer {token_b}"}

        res_tenant_b = client.get(
            f"/api/v1/graph/decisions/active?repo_identifier={repo_id}",
            headers=headers_b,
        )
        assert res_tenant_b.status_code == 200
        assert res_tenant_b.json()["markdown_content"] == ""


async def test_graph_ast_sync_logic(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """Verify that AST sync endpoint accepts payloads, upserts nodes, and detects drift."""
    tenant_a = UUID(setup_tokens["token_graph_a"])
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    repo_id = "github.com/company/sync-test"

    # 1. Seed database under Tenant A with an AST node and a linked canonical context
    async with tenant_context(engine, tenant_a) as conn_a:
        await conn_a.execute(text("SET LOCAL ROLE rls_test_user"))
        store_a = PostgresEventStore(conn_a)

        # Context
        ctx_id = uuid4()
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="Keep this method thread-safe",
            confidence_score=9.0,
            id=ctx_id,
        )

        # AST node
        await store_a.upsert_ast_node_and_check_drift(
            tenant_id=tenant_a,
            repo_identifier=repo_id,
            file_path="src/safe.py",
            symbol_name="thread_safe_func",
            node_type="function",
            merkle_hash="old_hash_val",
        )

        # Find the node ID
        nodes = await store_a.find_ast_nodes_by_symbols(tenant_a, ["thread_safe_func"])
        node_id = nodes[0]["id"]

        # Link context to AST node
        await store_a.link_ast_to_context(tenant_a, node_id, ctx_id)

    # 2. Call AST sync endpoint with a different hash (triggering drift detection)
    from fastapi.testclient import TestClient

    from checkcord.main import app

    token_a = "token_graph_a"
    headers_a = {"Authorization": f"Bearer {token_a}"}

    payload = {
        "repo_identifier": repo_id,
        "nodes": [
            {
                "file_path": "src/safe.py",
                "symbol_name": "thread_safe_func",
                "node_type": "function",
                "merkle_hash": "new_hash_val",  # Changed hash!
            },
            {
                "file_path": "src/new_file.py",
                "symbol_name": "new_func",
                "node_type": "function",
                "merkle_hash": "some_hash",
            },
        ],
    }

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/graph/ast/sync",
            json=payload,
            headers=headers_a,
        )
        assert res.status_code == 202
        assert res.json()["status"] == "accepted"

    # 3. Verify the changes in DB
    async with tenant_context(engine, tenant_a) as conn_a:
        await conn_a.execute(text("SET LOCAL ROLE rls_test_user"))
        store_a = PostgresEventStore(conn_a)

        # Verify new node is inserted
        nodes = await store_a.find_ast_nodes_by_symbols(tenant_a, ["new_func"])
        assert len(nodes) == 1
        assert nodes[0]["repo_identifier"] == repo_id
        assert nodes[0]["file_path"] == "src/new_file.py"
        assert nodes[0]["merkle_hash"] == "some_hash"

        # Verify old node hash is updated
        old_nodes = await store_a.find_ast_nodes_by_symbols(tenant_a, ["thread_safe_func"])
        assert len(old_nodes) == 1
        assert old_nodes[0]["merkle_hash"] == "new_hash_val"

        # Verify the linked context edge has drifted and is now marked "stale"
        # We can query context lookup for thread_safe_func, it should filter out stale contexts.
        contexts = await store_a.get_context_for_symbol(tenant_a, "thread_safe_func")
        # All contexts for thread_safe_func are filtered out because status == 'stale'
        assert len(contexts) == 0


async def test_graph_router_cors_headers() -> None:
    """Verify that graph router endpoints properly respond with CORS headers
    for frontend integration.
    """
    with TestClient(app) as client:
        # OPTIONS request (pre-flight)
        res = client.options(
            "/api/v1/graph/decisions/active?repo_identifier=test",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert res.status_code == 200
        assert res.headers.get("access-control-allow-origin") == "http://localhost:3000"
        assert "GET" in res.headers.get("access-control-allow-methods", "")


async def test_graph_network_endpoint(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """Verify retrieving the complete node/edge graph via the network API and RLS filtering."""
    tenant_a = UUID(setup_tokens["token_graph_a"])
    tenant_b = UUID(setup_tokens["token_graph_b"])
    from datetime import UTC, datetime

    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    # 1. Seed Tenant A Data
    async with tenant_context(engine, tenant_a) as conn_a:
        await conn_a.execute(text("SET LOCAL ROLE rls_test_user"))
        store_a = PostgresEventStore(conn_a)

        # Context
        ctx_id = uuid4()
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="Tenant A Context Rule",
            confidence_score=8.5,
            id=ctx_id,
        )

        # AST node
        await store_a.upsert_ast_node_and_check_drift(
            tenant_id=tenant_a,
            repo_identifier="github.com/org/repo-network",
            file_path="src/network.py",
            symbol_name="network_func",
            node_type="function",
            merkle_hash="hash999",
        )
        nodes = await store_a.find_ast_nodes_by_symbols(tenant_a, ["network_func"])
        node_id = nodes[0]["id"]

        # Link Context -> AST
        await store_a.link_ast_to_context(tenant_a, node_id, ctx_id)

        # Seed Event (to check Developer extraction)
        from checkcord.models.common import AccessControl, AuthorInfo
        from checkcord.models.enums import AccessVisibility, AclType, SourcePlatform, SourceType
        from checkcord.models.event import CanonicalEvent

        event_id = uuid4()
        event = CanonicalEvent(
            event_id=event_id,
            tenant_id=tenant_a,
            source_platform=SourcePlatform.SLACK,
            source_type=SourceType.MESSAGE,
            source_id="msg_123",
            happened_at=datetime.now(UTC),
            author=AuthorInfo(source_id="user_123", display_name="Dev Network A"),
            content_raw="raw msg",
            content_clean="clean msg",
            access_control=AccessControl(
                visibility=AccessVisibility.PUBLIC,
                acl_type=AclType.CUSTOM,
                is_public=True,
            ),
        )
        await store_a.save_event(event)
        # Link context to event
        await store_a.link_context_to_event(tenant_a, ctx_id, event_id)

    # 2. Seed Tenant B Data (to verify RLS works)
    async with tenant_context(engine, tenant_b) as conn_b:
        await conn_b.execute(text("SET LOCAL ROLE rls_test_user"))
        store_b = PostgresEventStore(conn_b)

        # Context
        ctx_b_id = uuid4()
        await store_b.save_canonical_context(
            tenant_id=tenant_b,
            summary="Tenant B Context Rule",
            confidence_score=9.0,
            id=ctx_b_id,
        )

        # AST node in Tenant B with same repo and files
        await store_b.upsert_ast_node_and_check_drift(
            tenant_id=tenant_b,
            repo_identifier="github.com/org/repo-network",
            file_path="src/network.py",
            symbol_name="network_func",
            node_type="function",
            merkle_hash="hash888",
        )
        nodes_b = await store_b.find_ast_nodes_by_symbols(tenant_b, ["network_func"])
        node_b_id = nodes_b[0]["id"]

        # Link Context -> AST
        await store_b.link_ast_to_context(tenant_b, node_b_id, ctx_b_id)

        # Seed Event (to check Developer extraction for B)
        event_b_id = uuid4()
        event_b = CanonicalEvent(
            event_id=event_b_id,
            tenant_id=tenant_b,
            source_platform=SourcePlatform.SLACK,
            source_type=SourceType.MESSAGE,
            source_id="msg_456",
            happened_at=datetime.now(UTC),
            author=AuthorInfo(source_id="user_456", display_name="Dev Network B"),
            content_raw="raw msg b",
            content_clean="clean msg b",
            access_control=AccessControl(
                visibility=AccessVisibility.PUBLIC,
                acl_type=AclType.CUSTOM,
                is_public=True,
            ),
        )
        await store_b.save_event(event_b)
        await store_b.link_context_to_event(tenant_b, ctx_b_id, event_b_id)

    # Clean engine
    await engine.dispose()

    # 3. Call endpoint as Tenant A
    with TestClient(app) as client:
        res_a = client.get(
            "/api/v1/graph/network?repo_identifier=github.com/org/repo-network",
            headers={"Authorization": "Bearer token_graph_a"},
        )
        assert res_a.status_code == 200
        data_a = res_a.json()

        # Verify Tenant A data
        nodes = data_a["nodes"]
        edges = data_a["edges"]

        # Nodes should have file, symbol, context, and developer
        node_ids = {n["id"] for n in nodes}
        assert "file:src/network.py" in node_ids
        assert f"symbol:{str(node_id)}" in node_ids
        assert f"context:{str(ctx_id)}" in node_ids
        assert "developer:Dev Network A" in node_ids

        # We must NOT see Tenant B nodes
        assert f"symbol:{str(node_b_id)}" not in node_ids
        assert f"context:{str(ctx_b_id)}" not in node_ids
        assert "developer:Dev Network B" not in node_ids

        # Verify edges
        edge_pairs = {(e["source"], e["target"]) for e in edges}
        assert ("file:src/network.py", f"symbol:{str(node_id)}") in edge_pairs
        assert (f"symbol:{str(node_id)}", f"context:{str(ctx_id)}") in edge_pairs
        assert (f"context:{str(ctx_id)}", "developer:Dev Network A") in edge_pairs

        # We must NOT see Tenant B edges
        assert ("file:src/network.py", f"symbol:{str(node_b_id)}") not in edge_pairs
        assert (f"symbol:{str(node_b_id)}", f"context:{str(ctx_b_id)}") not in edge_pairs
        assert (f"context:{str(ctx_b_id)}", "developer:Dev Network B") not in edge_pairs

        # 4. Call endpoint as Tenant B
        res_b = client.get(
            "/api/v1/graph/network?repo_identifier=github.com/org/repo-network",
            headers={"Authorization": "Bearer token_graph_b"},
        )
        assert res_b.status_code == 200
        data_b = res_b.json()

        # Verify Tenant B data
        nodes_b_res = data_b["nodes"]
        _ = data_b["edges"]
        node_ids_b = {n["id"] for n in nodes_b_res}

        # We must see Tenant B nodes
        assert "file:src/network.py" in node_ids_b
        assert f"symbol:{str(node_b_id)}" in node_ids_b
        assert f"context:{str(ctx_b_id)}" in node_ids_b
        assert "developer:Dev Network B" in node_ids_b

        # We must NOT see Tenant A nodes
        assert f"symbol:{str(node_id)}" not in node_ids_b
        assert f"context:{str(ctx_id)}" not in node_ids_b
        assert "developer:Dev Network A" not in node_ids_b


async def test_graph_node_details_endpoint(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """Verify retrieving detailed metadata for a single node via the node details
    API and RLS filtering.
    """
    tenant_a = UUID(setup_tokens["token_graph_a"])
    tenant_b = UUID(setup_tokens["token_graph_b"])
    from datetime import UTC, datetime

    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    # 1. Seed Tenant A Data
    async with tenant_context(engine, tenant_a) as conn_a:
        await conn_a.execute(text("SET LOCAL ROLE rls_test_user"))
        store_a = PostgresEventStore(conn_a)

        # Context
        ctx_id = uuid4()
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="Tenant A Detail Rule",
            confidence_score=8.5,
            id=ctx_id,
        )

        # AST node
        await store_a.upsert_ast_node_and_check_drift(
            tenant_id=tenant_a,
            repo_identifier="github.com/org/repo-network",
            file_path="src/detail.py",
            symbol_name="detail_func",
            node_type="function",
            merkle_hash="hash_detail",
        )
        nodes = await store_a.find_ast_nodes_by_symbols(tenant_a, ["detail_func"])
        node_id = nodes[0]["id"]

        # Link Context -> AST
        await store_a.link_ast_to_context(tenant_a, node_id, ctx_id)

        # Seed Event (to check Developer extraction & Context sources)
        from checkcord.models.common import AccessControl, AuthorInfo
        from checkcord.models.enums import AccessVisibility, AclType, SourcePlatform, SourceType
        from checkcord.models.event import CanonicalEvent

        event_id = uuid4()
        event = CanonicalEvent(
            event_id=event_id,
            tenant_id=tenant_a,
            source_platform=SourcePlatform.SLACK,
            source_type=SourceType.MESSAGE,
            source_id="msg_456",
            happened_at=datetime.now(UTC),
            author=AuthorInfo(source_id="user_456", display_name="Dev Detail A"),
            content_raw="raw msg detail",
            content_clean="clean msg detail",
            access_control=AccessControl(
                visibility=AccessVisibility.PUBLIC,
                acl_type=AclType.CUSTOM,
                is_public=True,
            ),
        )
        await store_a.save_event(event)
        # Link context to event
        await store_a.link_context_to_event(tenant_a, ctx_id, event_id)

    # 2. Seed Tenant B Data (to verify RLS works)
    async with tenant_context(engine, tenant_b) as conn_b:
        await conn_b.execute(text("SET LOCAL ROLE rls_test_user"))
        store_b = PostgresEventStore(conn_b)

        # Context
        ctx_b_id = uuid4()
        await store_b.save_canonical_context(
            tenant_id=tenant_b,
            summary="Tenant B Detail Rule",
            confidence_score=9.0,
            id=ctx_b_id,
        )

        # AST node
        await store_b.upsert_ast_node_and_check_drift(
            tenant_id=tenant_b,
            repo_identifier="github.com/org/repo-network",
            file_path="src/detail.py",
            symbol_name="detail_func_b",
            node_type="function",
            merkle_hash="hash_detail_b",
        )
        nodes_b = await store_b.find_ast_nodes_by_symbols(tenant_b, ["detail_func_b"])
        node_b_id = nodes_b[0]["id"]

        # Link Context -> AST
        await store_b.link_ast_to_context(tenant_b, node_b_id, ctx_b_id)

    # Clean up engine connection before using client
    await engine.dispose()

    # 3. Call endpoint as Tenant A
    with TestClient(app) as client:
        # A. File Node Detail
        res = client.get(
            "/api/v1/graph/node/file:src/detail.py?repo_identifier=github.com/org/repo-network",
            headers={"Authorization": "Bearer token_graph_a"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["type"] == "file"
        assert data["file_path"] == "src/detail.py"
        assert len(data["symbols"]) == 1
        assert data["symbols"][0]["symbol_name"] == "detail_func"

        # B. Symbol Node Detail
        res = client.get(
            f"/api/v1/graph/node/symbol:{str(node_id)}?repo_identifier=github.com/org/repo-network",
            headers={"Authorization": "Bearer token_graph_a"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["type"] == "symbol"
        assert data["symbol_name"] == "detail_func"
        assert len(data["contexts"]) == 1
        assert data["contexts"][0]["summary"] == "Tenant A Detail Rule"
        assert data["contexts"][0]["summary_redacted"] is False

        # C. Context Node Detail
        res = client.get(
            f"/api/v1/graph/node/context:{str(ctx_id)}?repo_identifier=github.com/org/repo-network",
            headers={"Authorization": "Bearer token_graph_a"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["type"] == "context"
        assert data["summary"] == "Tenant A Detail Rule"
        assert len(data["sources"]) == 1
        assert "content_clean" not in data["sources"][0]
        assert data["sources"][0]["source_platform"] == "slack"
        # content_hash/last_verified_at are computed by save_canonical_context
        # from the summary text at write time.
        assert data["content_hash"] == hashlib.sha256(b"Tenant A Detail Rule").hexdigest()
        assert data["last_verified_at"] is not None
        # No redaction occurred for this fixture (the autouse fixture is a
        # passthrough), so both indicators should be false.
        assert data["summary_redacted"] is False

        # D. Developer Node Detail
        res = client.get(
            "http://testserver/api/v1/graph/node/developer:Dev%20Detail%20A?repo_identifier=github.com/org/repo-network",
            headers={"Authorization": "Bearer token_graph_a"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["type"] == "developer"
        assert data["display_name"] == "Dev Detail A"
        assert data["authored_events_count"] == 1

        # E. Cross-Tenant RLS Check (Tenant A trying to get Tenant B's symbol
        # details should 404/not found)
        res_cross = client.get(
            f"/api/v1/graph/node/symbol:{str(node_b_id)}?repo_identifier=github.com/org/repo-network",
            headers={"Authorization": "Bearer token_graph_a"},
        )
        assert res_cross.status_code == 404

        # F. Cross-Tenant RLS Check (Tenant A trying to get Tenant B's context
        # details should 404/not found)
        res_cross_ctx = client.get(
            f"/api/v1/graph/node/context:{str(ctx_b_id)}?repo_identifier=github.com/org/repo-network",
            headers={"Authorization": "Bearer token_graph_a"},
        )
        assert res_cross_ctx.status_code == 404


async def test_graph_node_details_context_scope_repo_identifiers(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """get_node_details's context branch reports scope_repo_identifiers as a
    list: [] for org-global, the full set for a multi-repo-scoped context.
    """
    tenant_a = UUID(setup_tokens["token_graph_a"])
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    org_global_id = uuid4()
    multi_scoped_id = uuid4()
    async with tenant_context(engine, tenant_a) as conn_a:
        await conn_a.execute(text("SET LOCAL ROLE rls_test_user"))
        store_a = PostgresEventStore(conn_a)
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="Org-global decision",
            confidence_score=5.0,
            id=org_global_id,
        )
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="Decision scoped to two repos",
            confidence_score=6.0,
            id=multi_scoped_id,
        )
        await store_a.update_context_scope(
            tenant_id=tenant_a,
            canonical_id=multi_scoped_id,
            repo_identifiers=["acme/service-a", "acme/service-b"],
            is_locked=False,
            changed_by="alice@acme.example",
        )
    await engine.dispose()

    with TestClient(app) as client:
        headers_a = {"Authorization": "Bearer token_graph_a"}

        res_org_global = client.get(
            f"/api/v1/graph/node/context:{str(org_global_id)}?repo_identifier=irrelevant",
            headers=headers_a,
        )
        assert res_org_global.status_code == 200
        assert res_org_global.json()["scope_repo_identifiers"] == []

        res_multi = client.get(
            f"/api/v1/graph/node/context:{str(multi_scoped_id)}?repo_identifier=irrelevant",
            headers=headers_a,
        )
        assert res_multi.status_code == 200
        assert sorted(res_multi.json()["scope_repo_identifiers"]) == [
            "acme/service-a",
            "acme/service-b",
        ]


@pytest.mark.asyncio
async def test_graph_node_details_context_type_field(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """get_node_details's context branch returns context_type: 'general' by
    default, the real value for a typed (e.g. ADR) context.
    """
    tenant_a = UUID(setup_tokens["token_graph_a"])
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    general_id = uuid4()
    adr_id = uuid4()
    async with tenant_context(engine, tenant_a) as conn_a:
        await conn_a.execute(text("SET LOCAL ROLE rls_test_user"))
        store_a = PostgresEventStore(conn_a)
        await store_a.save_canonical_context(
            tenant_id=tenant_a, summary="Untyped decision", confidence_score=5.0, id=general_id
        )
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="Decision: adopt Postgres",
            confidence_score=6.0,
            id=adr_id,
            context_type="adr",
        )
    await engine.dispose()

    with TestClient(app) as client:
        headers_a = {"Authorization": "Bearer token_graph_a"}

        res_general = client.get(
            f"/api/v1/graph/node/context:{general_id}?repo_identifier=irrelevant",
            headers=headers_a,
        )
        assert res_general.status_code == 200
        assert res_general.json()["context_type"] == "general"

        res_adr = client.get(
            f"/api/v1/graph/node/context:{adr_id}?repo_identifier=irrelevant",
            headers=headers_a,
        )
        assert res_adr.status_code == 200
        assert res_adr.json()["context_type"] == "adr"


async def test_graph_node_details_context_redacts_summary_and_serves_no_source_text(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """A context node's summary is redacted, its sources carry no text and
    author.display_name is left untouched.
    """
    from datetime import UTC, datetime

    from checkcord.models.common import AccessControl, AuthorInfo
    from checkcord.models.enums import AccessVisibility, AclType, SourcePlatform, SourceType
    from checkcord.models.event import CanonicalEvent

    tenant_a = UUID(setup_tokens["token_graph_a"])

    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    async with tenant_context(engine, tenant_a) as conn_a:
        await conn_a.execute(text("SET LOCAL ROLE rls_test_user"))
        store_a = PostgresEventStore(conn_a)

        ctx_id = uuid4()
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="Contact alice@acme.example about this rule",
            confidence_score=8.5,
            id=ctx_id,
        )

        event_id = uuid4()
        event = CanonicalEvent(
            event_id=event_id,
            tenant_id=tenant_a,
            source_platform=SourcePlatform.SLACK,
            source_type=SourceType.MESSAGE,
            source_id="msg_redact",
            happened_at=datetime.now(UTC),
            author=AuthorInfo(source_id="user_redact", display_name="Alice Redactee"),
            content_raw="raw",
            content_clean="my token is sk-abcdefghijklmnopqrstuvwxyz123456",
            access_control=AccessControl(
                visibility=AccessVisibility.PUBLIC, acl_type=AclType.CUSTOM, is_public=True
            ),
        )
        await store_a.save_event(event)
        await store_a.link_context_to_event(tenant_a, ctx_id, event_id)

    await engine.dispose()

    with TestClient(app) as client:
        res = client.get(
            f"/api/v1/graph/node/context:{str(ctx_id)}?repo_identifier=irrelevant",
            headers={"Authorization": "Bearer token_graph_a"},
        )
        assert res.status_code == 200
        data = res.json()

    assert data["summary"] == "Contact [REDACTED:EMAIL] about this rule"
    assert data["summary_redacted"] is True
    assert "my token is" not in res.text
    assert data["sources"][0]["author"] == {"display_name": "Alice Redactee"}


async def test_graph_node_details_redaction_flags_are_independent_per_field(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """A clean summary reports summary_redacted=False whatever its source held."""
    from datetime import UTC, datetime

    from checkcord.models.common import AccessControl, AuthorInfo
    from checkcord.models.enums import AccessVisibility, AclType, SourcePlatform, SourceType
    from checkcord.models.event import CanonicalEvent

    tenant_a = UUID(setup_tokens["token_graph_a"])

    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    async with tenant_context(engine, tenant_a) as conn_a:
        await conn_a.execute(text("SET LOCAL ROLE rls_test_user"))
        store_a = PostgresEventStore(conn_a)

        ctx_id = uuid4()
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="This summary has no PII in it at all.",
            confidence_score=8.5,
            id=ctx_id,
        )

        event_id = uuid4()
        event = CanonicalEvent(
            event_id=event_id,
            tenant_id=tenant_a,
            source_platform=SourcePlatform.SLACK,
            source_type=SourceType.MESSAGE,
            source_id="msg_partial_redact",
            happened_at=datetime.now(UTC),
            author=AuthorInfo(source_id="user_partial", display_name="Bob Partial"),
            content_raw="raw",
            content_clean="reach me at bob@acme.example",
            access_control=AccessControl(
                visibility=AccessVisibility.PUBLIC, acl_type=AclType.CUSTOM, is_public=True
            ),
        )
        await store_a.save_event(event)
        await store_a.link_context_to_event(tenant_a, ctx_id, event_id)

    await engine.dispose()

    with TestClient(app) as client:
        res = client.get(
            f"/api/v1/graph/node/context:{str(ctx_id)}?repo_identifier=irrelevant",
            headers={"Authorization": "Bearer token_graph_a"},
        )
        assert res.status_code == 200
        data = res.json()

    assert data["summary"] == "This summary has no PII in it at all."
    assert data["summary_redacted"] is False
    assert "reach me" not in res.text and "bob@acme.example" not in res.text


@pytest.mark.asyncio
async def test_graph_ping_endpoint(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """Test graph ping endpoint to verify that access checks enforce RLS boundaries."""
    from checkcord.storage.postgres import PostgresEventStore, tenant_context

    tenant_a = UUID(setup_tokens["token_graph_a"])
    tenant_b = UUID(setup_tokens["token_graph_b"])

    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    async with tenant_context(engine, tenant_a) as conn_a:
        await conn_a.execute(text("SET LOCAL ROLE rls_test_user"))
        store_a = PostgresEventStore(conn_a)

        # Seed AST Node for Tenant A
        await store_a.upsert_ast_node_and_check_drift(
            tenant_id=tenant_a,
            repo_identifier="github.com/org/repo-a",
            file_path="src/main.py",
            symbol_name="main_func",
            node_type="function",
            merkle_hash="hash_main_a",
        )

    async with tenant_context(engine, tenant_b) as conn_b:
        await conn_b.execute(text("SET LOCAL ROLE rls_test_user"))
        store_b = PostgresEventStore(conn_b)

        # Seed AST Node for Tenant B
        await store_b.upsert_ast_node_and_check_drift(
            tenant_id=tenant_b,
            repo_identifier="github.com/org/repo-b",
            file_path="src/lib.py",
            symbol_name="lib_func",
            node_type="function",
            merkle_hash="hash_lib_b",
        )

    # Clean up engine connection before using test client
    await engine.dispose()

    # 2. Call ping endpoint and assert status codes
    with TestClient(app) as client:
        # A. Tenant A queries its own repo -> 200 OK
        res = client.get(
            "/api/v1/graph/ping?repo_identifier=github.com/org/repo-a",
            headers={"Authorization": "Bearer token_graph_a"},
        )
        assert res.status_code == 200
        assert res.json() == {"status": "ok", "repo_identifier": "github.com/org/repo-a"}

        # B. Tenant A queries Tenant B's repo -> 403 Forbidden
        res_forbidden = client.get(
            "/api/v1/graph/ping?repo_identifier=github.com/org/repo-b",
            headers={"Authorization": "Bearer token_graph_a"},
        )
        assert res_forbidden.status_code == 403

        # C. Tenant B queries Tenant A's repo -> 403 Forbidden
        res_forbidden_b = client.get(
            "/api/v1/graph/ping?repo_identifier=github.com/org/repo-a",
            headers={"Authorization": "Bearer token_graph_b"},
        )
        assert res_forbidden_b.status_code == 403


@pytest.mark.asyncio
async def test_graph_repositories_lists_only_the_callers_tenant(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    tenant_a = UUID(setup_tokens["token_graph_a"])
    tenant_b = UUID(setup_tokens["token_graph_b"])
    engine = create_async_engine(get_settings().database_url)
    seeds = [
        (tenant_a, "github.com/org/repo-a", "src/main.py", "main"),
        (tenant_a, "github.com/org/repo-a", "src/main.py", "helper"),
        (tenant_a, "github.com/org/repo-a", "src/util.py", "util"),
        (tenant_b, "github.com/org/repo-b", "src/lib.py", "lib"),
    ]
    for tenant, repo, path, symbol in seeds:
        async with tenant_context(engine, tenant) as conn:
            await conn.execute(text("SET LOCAL ROLE rls_test_user"))
            await PostgresEventStore(conn).upsert_ast_node_and_check_drift(
                tenant_id=tenant,
                repo_identifier=repo,
                file_path=path,
                symbol_name=symbol,
                node_type="function",
                merkle_hash=f"hash_{symbol}",
            )
    await engine.dispose()

    with TestClient(app) as client:
        res = client.get(
            "/api/v1/graph/repositories", headers={"Authorization": "Bearer token_graph_a"}
        )
        assert res.status_code == 200
        assert res.json() == {"items": [{"repo_identifier": "github.com/org/repo-a", "files": 2}]}
        assert client.get("/api/v1/graph/repositories").status_code == 401


@pytest.mark.asyncio
async def test_update_context_scope_success(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """A human can re-scope a context, and the change is audit-logged."""
    tenant_a = UUID(setup_tokens["token_graph_a"])
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    ctx_id = uuid4()
    async with tenant_context(engine, tenant_a) as conn_a:
        store_a = PostgresEventStore(conn_a)
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="A decision that was mis-scoped",
            confidence_score=5.0,
            id=ctx_id,
        )
    await engine.dispose()

    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/records/{ctx_id}/scope",
            json={
                "repo_identifiers": ["acme/service-a"],
                "is_locked": True,
                "changed_by": "alice@acme.example",
            },
            headers=_admin_as("alice@acme.example"),
        )
        assert res.status_code == 200
        data = res.json()
        assert data["id"] == str(ctx_id)
        assert data["scope_repo_identifiers"] == ["acme/service-a"]
        assert data["is_locked"] is True

    # Verify the audit log recorded the prior (fail-closed default) values.
    engine2 = create_async_engine(settings.database_url)
    async with tenant_context(engine2, tenant_a) as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT changed_by, previous_scope_repo_identifiers, "
                        "new_scope_repo_identifiers, previous_is_locked, new_is_locked "
                        "FROM context_scope_audit_log WHERE canonical_id = :cid"
                    ),
                    {"cid": ctx_id},
                )
            )
            .mappings()
            .fetchall()
        )
    await engine2.dispose()

    assert len(rows) == 1
    audit = rows[0]
    assert audit["changed_by"] == "alice@acme.example"
    assert audit["previous_scope_repo_identifiers"] is None
    assert audit["new_scope_repo_identifiers"] == ["acme/service-a"]
    assert audit["previous_is_locked"] is False
    assert audit["new_is_locked"] is True


@pytest.mark.asyncio
async def test_update_context_scope_to_multiple_repos(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """A context can be scoped to more than one repo."""
    tenant_a = UUID(setup_tokens["token_graph_a"])
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    ctx_id = uuid4()
    async with tenant_context(engine, tenant_a) as conn_a:
        store_a = PostgresEventStore(conn_a)
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="A shared-library ADR referenced from two services",
            confidence_score=7.0,
            id=ctx_id,
        )
    await engine.dispose()

    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/records/{ctx_id}/scope",
            json={
                "repo_identifiers": ["acme/service-a", "acme/service-b"],
                "is_locked": False,
                "changed_by": "alice@acme.example",
            },
            headers=_admin_as("alice@acme.example"),
        )
        assert res.status_code == 200
        data = res.json()
        assert sorted(data["scope_repo_identifiers"]) == ["acme/service-a", "acme/service-b"]

    engine2 = create_async_engine(settings.database_url)
    async with tenant_context(engine2, tenant_a) as conn:
        store_a2 = PostgresEventStore(conn)
        scopes = _repo_scopes(await store_a2.get_context_scope_rows(tenant_a, ctx_id))
    await engine2.dispose()
    assert sorted(scopes) == ["acme/service-a", "acme/service-b"]


@pytest.mark.asyncio
async def test_update_context_scope_full_replace_drops_stale_repos(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """repo_identifiers is a full replace: a second PATCH naming fewer repos
    leaves exactly the new set, no leftover rows.
    """
    tenant_a = UUID(setup_tokens["token_graph_a"])
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    ctx_id = uuid4()
    async with tenant_context(engine, tenant_a) as conn_a:
        store_a = PostgresEventStore(conn_a)
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="Scope narrowed after a correction",
            confidence_score=6.0,
            id=ctx_id,
        )
    await engine.dispose()

    with TestClient(app) as client:
        res1 = client.patch(
            f"/api/v1/admin/records/{ctx_id}/scope",
            json={
                "repo_identifiers": ["acme/service-a", "acme/service-b"],
                "is_locked": False,
                "changed_by": "alice@acme.example",
            },
            headers=_admin_as("alice@acme.example"),
        )
        assert res1.status_code == 200

        res2 = client.patch(
            f"/api/v1/admin/records/{ctx_id}/scope",
            json={
                "repo_identifiers": ["acme/service-a"],
                "is_locked": False,
                "changed_by": "bob@acme.example",
            },
            headers=_admin_as("bob@acme.example"),
        )
        assert res2.status_code == 200
        assert res2.json()["scope_repo_identifiers"] == ["acme/service-a"]

    engine2 = create_async_engine(settings.database_url)
    async with tenant_context(engine2, tenant_a) as conn:
        store_a2 = PostgresEventStore(conn)
        scopes = _repo_scopes(await store_a2.get_context_scope_rows(tenant_a, ctx_id))
    await engine2.dispose()
    # acme/service-b must be gone entirely, not left as a stale row.
    assert scopes == ["acme/service-a"]


@pytest.mark.asyncio
async def test_update_context_scope_second_change_chains_from_first(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """Two sequential edits produce two audit rows, chained previous->new."""
    tenant_a = UUID(setup_tokens["token_graph_a"])
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    ctx_id = uuid4()
    async with tenant_context(engine, tenant_a) as conn_a:
        store_a = PostgresEventStore(conn_a)
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="Re-scoped twice",
            confidence_score=5.0,
            id=ctx_id,
        )
    await engine.dispose()

    with TestClient(app) as client:
        res1 = client.patch(
            f"/api/v1/admin/records/{ctx_id}/scope",
            json={
                "repo_identifiers": ["acme/service-a"],
                "is_locked": False,
                "changed_by": "alice@acme.example",
            },
            headers=_admin_as("alice@acme.example"),
        )
        assert res1.status_code == 200

        res2 = client.patch(
            f"/api/v1/admin/records/{ctx_id}/scope",
            json={
                "repo_identifiers": None,
                "is_locked": True,
                "changed_by": "bob@acme.example",
            },
            headers=_admin_as("bob@acme.example"),
        )
        assert res2.status_code == 200
        assert res2.json()["scope_repo_identifiers"] == []
        assert res2.json()["is_locked"] is True

    engine2 = create_async_engine(settings.database_url)
    async with tenant_context(engine2, tenant_a) as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT changed_by, previous_scope_repo_identifiers, "
                        "new_scope_repo_identifiers, previous_is_locked, new_is_locked "
                        "FROM context_scope_audit_log WHERE canonical_id = :cid "
                        "ORDER BY changed_at ASC"
                    ),
                    {"cid": ctx_id},
                )
            )
            .mappings()
            .fetchall()
        )
    await engine2.dispose()

    assert len(rows) == 2
    first, second = rows
    assert first["changed_by"] == "alice@acme.example"
    assert first["previous_scope_repo_identifiers"] is None
    assert first["new_scope_repo_identifiers"] == ["acme/service-a"]

    assert second["changed_by"] == "bob@acme.example"
    # Second change's "previous" must match the first change's "new" -- the chain holds.
    assert second["previous_scope_repo_identifiers"] == first["new_scope_repo_identifiers"]
    assert second["previous_is_locked"] == first["new_is_locked"]
    assert second["new_scope_repo_identifiers"] is None
    assert second["new_is_locked"] is True


@pytest.mark.asyncio
async def test_update_context_scope_not_found(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """Patching a nonexistent context returns 404, not a silent no-op."""
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/records/{uuid4()}/scope",
            json={
                "repo_identifiers": ["acme/service-a"],
                "is_locked": False,
                "changed_by": "alice@acme.example",
            },
            headers=_admin_as("alice@acme.example"),
        )
        assert res.status_code == 404


@pytest.mark.asyncio
async def test_update_context_scope_tenant_isolation(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """Tenant B must not be able to re-scope Tenant A's context."""
    tenant_a = UUID(setup_tokens["token_graph_a"])
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    ctx_id = uuid4()
    async with tenant_context(engine, tenant_a) as conn_a:
        store_a = PostgresEventStore(conn_a)
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="Tenant A's context",
            confidence_score=5.0,
            id=ctx_id,
        )
    await engine.dispose()

    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/records/{ctx_id}/scope",
            json={
                "repo_identifiers": ["evil/repo"],
                "is_locked": False,
                "changed_by": "mallory@evil.example",
            },
            headers=_admin_b(),
        )
        assert res.status_code == 404

    # Confirm the context was genuinely untouched.
    engine2 = create_async_engine(settings.database_url)
    async with tenant_context(engine2, tenant_a) as conn:
        store_a2 = PostgresEventStore(conn)
        ctx = await store_a2.get_canonical_context(tenant_a, ctx_id)
        scopes = _repo_scopes(await store_a2.get_context_scope_rows(tenant_a, ctx_id))
    await engine2.dispose()
    assert ctx is not None
    assert scopes == []


async def _seed_scoped_context(tenant_id: UUID, summary: str) -> UUID:
    engine = create_async_engine(get_settings().database_url)
    ctx_id = uuid4()
    async with tenant_context(engine, tenant_id) as conn:
        await PostgresEventStore(conn).save_canonical_context(
            tenant_id=tenant_id, summary=summary, confidence_score=5.0, id=ctx_id
        )
    await engine.dispose()
    return ctx_id


async def _scope_audit_actors(tenant_id: UUID, ctx_id: UUID) -> list[str]:
    engine = create_async_engine(get_settings().database_url)
    async with tenant_context(engine, tenant_id) as conn:
        rows = await conn.execute(
            text(
                "SELECT changed_by FROM context_scope_audit_log "
                "WHERE canonical_id = :cid ORDER BY changed_at ASC"
            ),
            {"cid": ctx_id},
        )
        actors = [r[0] for r in rows]
    await engine.dispose()
    return actors


@pytest.mark.asyncio
async def test_update_context_scope_rejects_ordinary_key(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    tenant_a = UUID(setup_tokens["token_graph_a"])
    ctx_id = await _seed_scoped_context(tenant_a, "Only admins may re-scope this")

    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/records/{ctx_id}/scope",
            json={"repo_identifiers": ["evil/repo"], "is_locked": False, "changed_by": "x"},
            headers={"Authorization": "Bearer token_graph_a"},
        )
        assert res.status_code == 403
        assert res.json()["detail"] == "Forbidden: admin rights required"

    assert await _scope_audit_actors(tenant_a, ctx_id) == []


@pytest.mark.asyncio
async def test_update_context_scope_actor_comes_from_key_not_payload(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    tenant_a = UUID(setup_tokens["token_graph_a"])
    ctx_id = await _seed_scoped_context(tenant_a, "Audit actor is the key's user")

    with TestClient(app) as client:
        res1 = client.patch(
            f"/api/v1/admin/records/{ctx_id}/scope",
            json={"repo_identifiers": ["acme/a"], "is_locked": False, "changed_by": "ceo@acme"},
            headers=_admin_as("alice@acme.example"),
        )
        assert res1.status_code == 200
        res2 = client.patch(
            f"/api/v1/admin/records/{ctx_id}/scope",
            json={"repo_identifiers": ["acme/b"], "is_locked": False},
            headers={"Authorization": "Bearer token_graph_admin_unbound"},
        )
        assert res2.status_code == 200
        history = client.get(
            f"/api/v1/graph/context/{ctx_id}/scope/history",
            headers={"Authorization": "Bearer token_graph_a"},
        )
        assert history.json()["changed_by"] == "key:test key"

    assert await _scope_audit_actors(tenant_a, ctx_id) == [
        "alice@acme.example",
        "key:test key",
    ]


# ---------------------------------------------------------------------------
# Four-tier scope_rows request shape and GET .../scope/history.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_context_scope_with_scope_rows_typed_request(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """scope_rows accepts a typed, multi-tier set in one PATCH."""
    tenant_a = UUID(setup_tokens["token_graph_a"])
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    ctx_id = uuid4()
    async with tenant_context(engine, tenant_a) as conn:
        store_a = PostgresEventStore(conn)
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="Context scoped via the typed request shape",
            confidence_score=5.0,
            id=ctx_id,
        )
    await engine.dispose()

    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/records/{ctx_id}/scope",
            json={
                "scope_rows": [
                    {"scope_type": "team", "scope_value": "backend"},
                    {"scope_type": "repo", "scope_value": "acme/service-a"},
                ],
                "is_locked": False,
                "changed_by": "alice@acme.example",
            },
            headers=_admin_as("alice@acme.example"),
        )
        assert res.status_code == 200
        data = res.json()
        assert data["id"] == str(ctx_id)
        assert data["scope_repo_identifiers"] == ["acme/service-a"]
        assert sorted(data["scope_rows"], key=lambda r: r["scope_type"]) == [
            {"scope_type": "repo", "scope_value": "acme/service-a"},
            {"scope_type": "team", "scope_value": "backend"},
        ]


@pytest.mark.asyncio
async def test_update_context_scope_scope_rows_takes_precedence_over_repo_identifiers(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """When both are given, scope_rows wins (documented precedence)."""
    tenant_a = UUID(setup_tokens["token_graph_a"])
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    ctx_id = uuid4()
    async with tenant_context(engine, tenant_a) as conn:
        store_a = PostgresEventStore(conn)
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="Both fields given",
            confidence_score=5.0,
            id=ctx_id,
        )
    await engine.dispose()

    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/records/{ctx_id}/scope",
            json={
                "repo_identifiers": ["acme/service-should-be-ignored"],
                "scope_rows": [{"scope_type": "team", "scope_value": "backend"}],
                "is_locked": False,
                "changed_by": "alice@acme.example",
            },
            headers=_admin_as("alice@acme.example"),
        )
        assert res.status_code == 200
        data = res.json()
        assert data["scope_repo_identifiers"] == []
        assert data["scope_rows"] == [{"scope_type": "team", "scope_value": "backend"}]


@pytest.mark.asyncio
async def test_update_context_scope_empty_scope_rows_means_org_global(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    tenant_a = UUID(setup_tokens["token_graph_a"])
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    ctx_id = uuid4()
    async with tenant_context(engine, tenant_a) as conn:
        store_a = PostgresEventStore(conn)
        await store_a.save_canonical_context(
            tenant_id=tenant_a,
            summary="Cleared back to org-global via scope_rows",
            confidence_score=5.0,
            id=ctx_id,
            scope_repo_identifier="acme/service-a",
        )
    await engine.dispose()

    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/records/{ctx_id}/scope",
            json={"scope_rows": [], "is_locked": False, "changed_by": "alice@acme.example"},
            headers=_admin_as("alice@acme.example"),
        )
        assert res.status_code == 200
        data = res.json()
        assert data["scope_repo_identifiers"] == []
        assert data["scope_rows"] == []


@pytest.mark.asyncio
async def test_get_context_scope_history_returns_null_when_never_edited(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    tenant_a = UUID(setup_tokens["token_graph_a"])
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    ctx_id = uuid4()
    async with tenant_context(engine, tenant_a) as conn:
        store_a = PostgresEventStore(conn)
        await store_a.save_canonical_context(
            tenant_id=tenant_a, summary="Never edited", confidence_score=5.0, id=ctx_id
        )
    await engine.dispose()

    with TestClient(app) as client:
        res = client.get(
            f"/api/v1/graph/context/{ctx_id}/scope/history",
            headers={"Authorization": "Bearer token_graph_a"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data == {"changed_by": None, "changed_at": None}


@pytest.mark.asyncio
async def test_get_context_scope_history_returns_the_most_recent_change(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """The UI's "last changed by X at Y" display reads this endpoint before an
    edit, so a conflicting concurrent edit is visible.
    """
    tenant_a = UUID(setup_tokens["token_graph_a"])
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    ctx_id = uuid4()
    async with tenant_context(engine, tenant_a) as conn:
        store_a = PostgresEventStore(conn)
        await store_a.save_canonical_context(
            tenant_id=tenant_a, summary="Edited by two people", confidence_score=5.0, id=ctx_id
        )
    await engine.dispose()

    with TestClient(app) as client:
        headers = {"Authorization": "Bearer token_graph_a"}
        client.patch(
            f"/api/v1/admin/records/{ctx_id}/scope",
            json={
                "repo_identifiers": ["acme/service-a"],
                "is_locked": False,
                "changed_by": "alice@acme.example",
            },
            headers=_admin_as("alice@acme.example"),
        )
        client.patch(
            f"/api/v1/admin/records/{ctx_id}/scope",
            json={
                "repo_identifiers": ["acme/service-b"],
                "is_locked": True,
                "changed_by": "bob@acme.example",
            },
            headers=_admin_as("bob@acme.example"),
        )

        res = client.get(f"/api/v1/graph/context/{ctx_id}/scope/history", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["changed_by"] == "bob@acme.example"
        assert data["changed_at"] is not None


@pytest.mark.asyncio
async def test_get_context_scope_history_requires_auth() -> None:
    with TestClient(app) as client:
        res = client.get(f"/api/v1/graph/context/{uuid4()}/scope/history")
        assert res.status_code == 401
