import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from checkcord.core.config import get_settings
from checkcord.core.identity import ANONYMOUS_USER_ID
from checkcord.services import mcp_tools
from checkcord.storage import audit

pytestmark = pytest.mark.asyncio

_call: dict[str, Any] = {}


@pytest.fixture(autouse=True)
def _caller() -> Iterator[None]:
    _call.update(engine=None, tenant=uuid.uuid4(), user_id=None, embedder=None)
    yield
    get_settings.cache_clear()


@contextmanager
def _using(**values: Any) -> Iterator[None]:
    _call.update(values)
    yield


def _actor() -> audit.Actor:
    user_id = _call["user_id"]
    if user_id == ANONYMOUS_USER_ID:
        user_id = None
    return audit.Actor(channel=audit.MCP, user_id=user_id)


async def get_dependency_flow(file_path: str) -> str:
    return await mcp_tools.dependency_flow(_call["engine"], _call["tenant"], _actor(), file_path)


async def view_active_decisions(**kwargs: Any) -> str:
    return await mcp_tools.active_decisions(_call["engine"], _call["tenant"], _actor(), **kwargs)


async def search_conceptual_context(query: str, **kwargs: Any) -> str:
    return await mcp_tools.conceptual_search(
        _call["engine"], _call["tenant"], _actor(), lambda: _call["embedder"], query, **kwargs
    )


async def test_get_dependency_flow_success(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_engine.begin.return_value = mock_context
    mock_result = MagicMock()
    # Mocking rows returned
    node = uuid.uuid4()
    mock_result.fetchall.return_value = [
        ("calculateHash", "function", node, "acme/api"),
        ("HashHelper", "class", uuid.uuid4(), "acme/api"),
    ]
    mock_conn.execute.return_value = mock_result
    record = {"id": str(uuid.uuid4()), "type": "adr", "severity": "blocking", "text": "t"}

    with (
        _using(engine=mock_engine),
        patch.object(
            mcp_tools.repo_audience,
            "readable_repositories",
            AsyncMock(return_value={"acme/api"}),
        ),
        patch.object(
            mcp_tools.code_links, "node_records", AsyncMock(return_value={node: [record]})
        ),
    ):
        result = await get_dependency_flow("src/auth.py")
        data = json.loads(result)

        assert data["file_path"] == "src/auth.py"
        assert len(data["nodes"]) == 2
        assert data["nodes"][0]["symbol_name"] == "calculateHash"
        assert data["nodes"][0]["node_type"] == "function"
        assert data["nodes"][1]["symbol_name"] == "HashHelper"
        assert data["nodes"][1]["node_type"] == "class"
        assert data["nodes"][0]["records"] == [record]
        assert data["nodes"][1]["records"] == []


async def test_get_dependency_flow_hides_unreadable_repository() -> None:
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_engine.begin.return_value = mock_context
    mock_result = MagicMock()
    mock_result.fetchall.return_value = [("quote_total", "function", uuid.uuid4(), "acme/pay")]
    mock_conn.execute.return_value = mock_result
    node_records = AsyncMock(return_value={})

    with (
        _using(engine=mock_engine),
        patch.object(
            mcp_tools.repo_audience, "readable_repositories", AsyncMock(return_value=set())
        ),
        patch.object(mcp_tools.code_links, "node_records", node_records),
    ):
        data = json.loads(await get_dependency_flow("charges.py"))
        data.pop("notice")

    assert data == {"nodes": []}
    assert node_records.await_args.args[2] == []


async def test_get_dependency_flow_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_engine.begin.return_value = mock_context
    mock_result = MagicMock()
    # No rows returned
    mock_result.fetchall.return_value = []
    mock_conn.execute.return_value = mock_result

    with _using(engine=mock_engine):
        result = await get_dependency_flow("src/empty.py")
        data = json.loads(result)

        assert "nodes" in data
        assert len(data["nodes"]) == 0


async def test_get_dependency_flow_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))

    mock_engine = MagicMock()
    mock_engine.begin.side_effect = Exception("DB Connection Failed")

    with _using(engine=mock_engine):
        result = await get_dependency_flow("src/test.py")
        data = json.loads(result)

        assert "error" in data
        assert "Failed to get dependency flow: DB Connection Failed" in data["error"]


async def test_get_dependency_flow_absolute_path_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_engine.begin.return_value = mock_context
    mock_result = MagicMock()
    mock_result.fetchall.return_value = [("calculateHash", "function", uuid.uuid4(), "a/b")]
    mock_conn.execute.return_value = mock_result

    cwd = os.getcwd().replace("\\", "/")
    absolute_path = f"{cwd}/src/nested/dir/auth.py"

    with (
        _using(engine=mock_engine),
        patch.object(
            mcp_tools.repo_audience, "readable_repositories", AsyncMock(return_value={"a/b"})
        ),
        patch.object(mcp_tools.code_links, "node_records", AsyncMock(return_value={})),
    ):
        result = await get_dependency_flow(absolute_path)
        data = json.loads(result)

        assert data["file_path"] == "src/nested/dir/auth.py"
        assert len(data["nodes"]) == 1
        assert data["nodes"][0]["symbol_name"] == "calculateHash"


async def test_view_active_decisions_global_success(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_engine.begin.return_value = mock_context

    mock_store = AsyncMock()
    mock_store.get_org_global_top_contexts.return_value = [
        {
            "id": str(uuid.uuid4()),
            "summary": "Use Postgres for storage",
            "confidence_score": 0.95,
            "content_hash": "abc123",
            "last_verified_at": "2026-07-29T00:00:00+00:00",
            "sources": [],
        },
        {
            "id": str(uuid.uuid4()),
            "summary": "Use Redis for streaming",
            "confidence_score": 0.85,
            "content_hash": "def456",
            "last_verified_at": "2026-07-29T00:00:00+00:00",
            "sources": [],
        },
    ]

    with _using(engine=mock_engine):
        with patch("checkcord.storage.postgres.PostgresEventStore", return_value=mock_store):
            result = await view_active_decisions()
            data = json.loads(result)

        assert "decisions" in data
        assert len(data["decisions"]) == 2
        assert data["decisions"][0]["summary"] == "Use Postgres for storage"
        assert data["decisions"][0]["confidence_score"] == 0.95
        assert data["decisions"][0]["content_hash"] == "abc123"
        assert data["decisions"][0]["sources"] == []
        mock_store.get_org_global_top_contexts.assert_called_once_with(
            uuid.UUID(tenant_id), limit=10, team_identifier=None, path=None, decisions_only=True
        )


async def test_view_active_decisions_repo_success(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_engine.begin.return_value = mock_context

    mock_store = AsyncMock()
    mock_store.get_top_contexts_for_repo.return_value = [
        {"summary": "Auth token format", "confidence_score": 0.99}
    ]

    with _using(engine=mock_engine):
        with patch("checkcord.storage.postgres.PostgresEventStore", return_value=mock_store):
            result = await view_active_decisions(repo_identifier="my-repo")
            data = json.loads(result)

            assert data["repo_identifier"] == "my-repo"
            assert len(data["decisions"]) == 1
            assert data["decisions"][0]["summary"] == "Auth token format"


async def test_view_active_decisions_passes_the_limit_to_both_queries() -> None:
    _call["tenant"] = uuid.uuid4()
    mock_engine = MagicMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = AsyncMock()
    mock_engine.begin.return_value = mock_context
    mock_store = AsyncMock()
    mock_store.get_org_global_top_contexts.return_value = []
    mock_store.get_top_contexts_for_repo.return_value = []

    with _using(engine=mock_engine):
        with patch("checkcord.storage.postgres.PostgresEventStore", return_value=mock_store):
            await view_active_decisions(limit=50)
            await view_active_decisions(repo_identifier="my-repo", limit=1)

    assert mock_store.get_org_global_top_contexts.call_args.kwargs["limit"] == 50
    assert mock_store.get_top_contexts_for_repo.call_args.kwargs["limit"] == 1


@pytest.mark.parametrize("bad", [0, 51, -1, True, 2.5, "10"])
async def test_view_active_decisions_rejects_a_limit_out_of_range(bad: Any) -> None:
    _call["tenant"] = uuid.uuid4()
    mock_store = AsyncMock()
    with _using(engine=MagicMock()):
        with patch("checkcord.storage.postgres.PostgresEventStore", return_value=mock_store):
            data = json.loads(await view_active_decisions(limit=bad))

    assert data == {
        "notice": mcp_tools.NOTICE,
        "error": "limit must be a whole number from 1 to 50",
    }
    mock_store.get_org_global_top_contexts.assert_not_called()


async def test_view_active_decisions_states_its_order_and_limit() -> None:
    assert (
        "Blocking decisions come first, then highest evidence score, then newest."
        in mcp_tools.ACTIVE_DECISIONS
    )
    assert "limit: How many decisions to return, 1 to 50. Default 10." in mcp_tools.ACTIVE_DECISIONS


@pytest.mark.integration
async def test_view_active_decisions_no_repo_returns_org_global_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting repo_identifier must not return every repo's local rules, only
    org-global contexts. Real database.
    """
    from sqlalchemy import text

    from checkcord.core.config import get_settings
    from checkcord.storage.postgres import (
        PostgresEventStore,
        create_checkcord_engine,
        tenant_context,
    )

    settings = get_settings()
    tenant_id = uuid.uuid4()
    _call["tenant"] = tenant_id

    engine = create_checkcord_engine(settings.database_url)
    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Org-wide rule visible everywhere",
                confidence_score=9.0,
                context_type="adr",
            )
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Repo-local rule for service-a",
                confidence_score=9.5,
                context_type="adr",
                scope_repo_identifier="acme/service-a",
            )

        with _using(engine=engine):
            result = await view_active_decisions()
            data = json.loads(result)

        summaries = {d["summary"] for d in data["decisions"]}
        assert summaries == {"Org-wide rule visible everywhere"}
        assert "Repo-local rule for service-a" not in summaries
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


@pytest.mark.integration
async def test_view_active_decisions_repo_identifier_json_includes_receipt_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The JSON string returned by view_active_decisions(repo_identifier=...)
    contains content_hash/last_verified_at/sources for a seeded context, end to end
    (real DB -> store -> json.dumps -> json.loads).
    """
    from sqlalchemy import text

    from checkcord.core.config import get_settings
    from checkcord.storage.postgres import (
        PostgresEventStore,
        create_checkcord_engine,
        tenant_context,
    )

    settings = get_settings()
    tenant_id = uuid.uuid4()
    repo_id = "acme/service-a"
    _call["tenant"] = tenant_id

    engine = create_checkcord_engine(settings.database_url)
    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Repo-scoped decision with a receipt",
                confidence_score=9.0,
                context_type="adr",
                scope_repo_identifier=repo_id,
            )

        with _using(engine=engine):
            result = await view_active_decisions(repo_identifier=repo_id)
            data = json.loads(result)

        assert len(data["decisions"]) == 1
        decision = data["decisions"][0]
        assert decision["content_hash"]
        assert decision["last_verified_at"]
        assert decision["sources"] == []
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


@pytest.mark.integration
async def test_view_active_decisions_returns_decisions_not_reference_records() -> None:
    from sqlalchemy import text

    from checkcord.storage.postgres import (
        PostgresEventStore,
        create_checkcord_engine,
        tenant_context,
    )

    tenant_id = uuid.uuid4()
    _call["tenant"] = tenant_id
    engine = create_checkcord_engine(get_settings().database_url)
    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            for i in range(10):
                await store.save_canonical_context(
                    tenant_id=tenant_id, summary=f"Ticket text {i}", confidence_score=50.0
                )
            for kind in ("adr", "risk"):
                await store.save_canonical_context(
                    tenant_id=tenant_id,
                    summary=f"A {kind} decision",
                    confidence_score=1.0,
                    context_type=kind,
                )

        with _using(engine=engine):
            data = json.loads(await view_active_decisions())

        assert data["notice"] == mcp_tools.NOTICE
        assert sorted((d["type"], d["summary"]) for d in data["decisions"]) == [
            ("adr", "A adr decision"),
            ("risk", "A risk decision"),
        ]
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_every_tool_result_carries_the_notice() -> None:
    with _using(engine=MagicMock(begin=MagicMock(side_effect=Exception("down")))):
        results = [
            await get_dependency_flow("a.py"),
            await view_active_decisions(),
            await view_active_decisions(limit=0),
            await search_conceptual_context(""),
        ]
    assert all(json.loads(r)["notice"] == mcp_tools.NOTICE for r in results)
    for description in (
        mcp_tools.DEPENDENCY_FLOW,
        mcp_tools.ACTIVE_DECISIONS,
        mcp_tools.CONCEPTUAL_SEARCH,
    ):
        assert mcp_tools.NOTICE in " ".join(description.split())


async def test_view_active_decisions_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))

    mock_engine = MagicMock()
    mock_engine.begin.side_effect = Exception("DB connection timeout")

    with _using(engine=mock_engine):
        result = await view_active_decisions()
        data = json.loads(result)

        assert "error" in data
        assert "Failed to retrieve active decisions" in data["error"]


async def test_search_conceptual_context_success(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_engine.begin.return_value = mock_context

    mock_store = AsyncMock()
    mock_store.find_similar_contexts.return_value = [
        {"summary": "Slack decision on DB", "confidence_score": 0.92, "similarity": 0.88}
    ]

    mock_embedding_client = AsyncMock()
    mock_embedding_client.embed_text.return_value = [0.1, 0.2, 0.3]

    with _using(engine=mock_engine):
        with patch("checkcord.storage.postgres.PostgresEventStore", return_value=mock_store):
            with _using(embedder=mock_embedding_client):
                result = await search_conceptual_context("Why did we choose Postgres?")
                data = json.loads(result)

                mock_embedding_client.embed_text.assert_called_once_with(
                    "Why did we choose Postgres?"
                )
                assert data["query"] == "Why did we choose Postgres?"
                assert len(data["results"]) == 1
                assert data["results"][0]["summary"] == "Slack decision on DB"
                assert data["results"][0]["similarity"] == 0.88
                assert data["results"][0]["type"] == "general"
                assert data["results"][0]["severity"] == "advisory"
                mock_store.find_similar_contexts.assert_called_once_with(
                    uuid.UUID(tenant_id),
                    [0.1, 0.2, 0.3],
                    threshold=0.3,
                    repo_identifier=None,
                    query_text="Why did we choose Postgres?",
                    team_identifier=None,
                    path=None,
                )


async def test_search_conceptual_context_includes_receipt_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Results carry content_hash/last_verified_at/sources."""
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_engine.begin.return_value = mock_context

    context_id = str(uuid.uuid4())
    mock_store = AsyncMock()
    mock_store.find_similar_contexts.return_value = [
        {
            "id": context_id,
            "summary": "Slack decision on DB",
            "confidence_score": 0.92,
            "similarity": 0.88,
            "content_hash": "deadbeef",
            "last_verified_at": "2026-07-29T00:00:00+00:00",
            "sources": [
                {
                    "event_id": str(uuid.uuid4()),
                    "source_platform": "slack",
                    "source_type": "message",
                    "happened_at": "2026-07-28T00:00:00+00:00",
                }
            ],
        }
    ]

    mock_embedding_client = AsyncMock()
    mock_embedding_client.embed_text.return_value = [0.1, 0.2, 0.3]

    with _using(engine=mock_engine):
        with patch("checkcord.storage.postgres.PostgresEventStore", return_value=mock_store):
            with _using(embedder=mock_embedding_client):
                result = await search_conceptual_context("Why did we choose Postgres?")
                data = json.loads(result)

                decision = data["results"][0]
                assert decision["id"] == context_id
                assert decision["content_hash"] == "deadbeef"
                assert decision["last_verified_at"] == "2026-07-29T00:00:00+00:00"
                assert len(decision["sources"]) == 1
                assert decision["sources"][0]["source_platform"] == "slack"
                assert "content_clean" not in decision["sources"][0]


async def test_search_conceptual_context_passes_repo_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """search_conceptual_context's optional repo_identifier reaches the store."""
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_engine.begin.return_value = mock_context

    mock_store = AsyncMock()
    mock_store.find_similar_contexts.return_value = []

    mock_embedding_client = AsyncMock()
    mock_embedding_client.embed_text.return_value = [0.1, 0.2, 0.3]

    with _using(engine=mock_engine):
        with patch("checkcord.storage.postgres.PostgresEventStore", return_value=mock_store):
            with _using(embedder=mock_embedding_client):
                await search_conceptual_context(
                    "Why did we choose Postgres?", repo_identifier="acme/service-a"
                )

                mock_store.find_similar_contexts.assert_called_once_with(
                    uuid.UUID(tenant_id),
                    [0.1, 0.2, 0.3],
                    threshold=0.3,
                    repo_identifier="acme/service-a",
                    query_text="Why did we choose Postgres?",
                    team_identifier=None,
                    path=None,
                )


async def test_search_conceptual_context_embedding_error(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))

    mock_embedding_client = AsyncMock()
    mock_embedding_client.embed_text.side_effect = Exception("API key invalid")

    with _using(embedder=mock_embedding_client):
        result = await search_conceptual_context("Some query")
        data = json.loads(result)

        assert "error" in data
        assert "Failed to generate query embedding" in data["error"]


async def test_search_conceptual_context_no_embedding_provider_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No provider configured (missing API key) is a distinct, explicit error --
    not conflated with a mid-call embedding failure."""
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))

    with _using(embedder=None):
        result = await search_conceptual_context("Some query")
        data = json.loads(result)

        assert "error" in data
        assert "not configured" in data["error"]


async def test_search_conceptual_context_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))

    mock_engine = MagicMock()
    mock_engine.begin.side_effect = Exception("Postgres unreachable")
    mock_embedding_client = AsyncMock()
    mock_embedding_client.embed_text.return_value = [0.1, 0.2, 0.3]

    with _using(engine=mock_engine):
        with _using(embedder=mock_embedding_client):
            result = await search_conceptual_context("Some query")
            data = json.loads(result)

            assert "error" in data
            assert "Failed to search conceptual context" in data["error"]


# --- Per-user identity resolution ------------------------------------------


async def test_view_active_decisions_unset_identity_resolves_to_anonymous_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller with no person -> ANONYMOUS_USER_ID is threaded into
    tenant_context(), not a bare None (see core/identity.py for why)."""
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_engine.begin.return_value = mock_context

    mock_store = AsyncMock()
    mock_store.get_org_global_top_contexts.return_value = []

    with _using(engine=mock_engine):
        with patch("checkcord.storage.postgres.PostgresEventStore", return_value=mock_store):
            result = await view_active_decisions()
            data = json.loads(result)

    assert data == {"notice": mcp_tools.NOTICE, "decisions": []}
    # SET LOCAL app.current_user_id must have been executed with the sentinel,
    # not skipped (which is what a bare None user_id would do).
    executed_sql = [str(call.args[0]) for call in mock_conn.execute.call_args_list]
    assert any(str(ANONYMOUS_USER_ID) in sql for sql in executed_sql)


async def test_search_conceptual_context_resolved_identity_threaded_into_tenant_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller's person reaches tenant_context() as
    a distinct value from tenant_id.
    """
    tenant_id = str(uuid.uuid4())
    resolved_user_id = uuid.uuid4()
    _call["tenant"] = uuid.UUID(str(tenant_id))

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_engine.begin.return_value = mock_context

    mock_store = AsyncMock()
    mock_store.find_similar_contexts.return_value = []
    mock_embedding_client = AsyncMock()
    mock_embedding_client.embed_text.return_value = [0.1, 0.2, 0.3]

    with _using(engine=mock_engine):
        with _using(user_id=resolved_user_id):
            with patch("checkcord.storage.postgres.PostgresEventStore", return_value=mock_store):
                with _using(embedder=mock_embedding_client):
                    await search_conceptual_context("Why did we choose Postgres?")

    executed_sql = [str(call.args[0]) for call in mock_conn.execute.call_args_list]
    assert any(str(resolved_user_id) in sql for sql in executed_sql)
    assert any(tenant_id in sql for sql in executed_sql)


# --- PII redaction in MCP tool responses ------------------------------------


async def test_view_active_decisions_redacts_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_engine.begin.return_value = mock_context

    ctx_id = str(uuid.uuid4())
    mock_store = AsyncMock()
    mock_store.get_org_global_top_contexts.return_value = [
        {
            "id": ctx_id,
            "summary": "Contact alice@acme.example about this decision",
            "summary_redacted": "Contact [REDACTED:EMAIL] about this decision",
            "confidence_score": 0.9,
            "content_hash": "abc123",
            "last_verified_at": "2026-07-29T00:00:00+00:00",
            "sources": [],
        }
    ]

    record = AsyncMock()
    with _using(engine=mock_engine):
        with patch("checkcord.storage.postgres.PostgresEventStore", return_value=mock_store):
            with patch("checkcord.storage.audit.record", record):
                result = await view_active_decisions()
                data = json.loads(result)

    assert data["decisions"][0]["summary"] == "Contact [REDACTED:EMAIL] about this decision"
    record.assert_awaited_once()
    assert record.call_args.args[3] == "mcp.view_active_decisions"
    assert record.call_args.kwargs["record_ids"] == [ctx_id]
    assert record.call_args.kwargs["redacted"] == 1


async def test_search_conceptual_context_redacts_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_engine.begin.return_value = mock_context

    ctx_id = str(uuid.uuid4())
    mock_store = AsyncMock()
    mock_store.find_similar_contexts.return_value = [
        {
            "id": ctx_id,
            "summary": "Ping bob@acme.example for the token sk-abc123",
            "summary_redacted": "REDACTED SUMMARY",
            "confidence_score": 0.9,
            "similarity": 0.8,
            "content_hash": "abc123",
            "last_verified_at": "2026-07-29T00:00:00+00:00",
            "sources": [],
        }
    ]
    mock_embedding_client = AsyncMock()
    mock_embedding_client.embed_text.return_value = [0.1, 0.2, 0.3]

    with _using(engine=mock_engine):
        with patch("checkcord.storage.postgres.PostgresEventStore", return_value=mock_store):
            with _using(embedder=mock_embedding_client):
                with patch("checkcord.storage.audit.record", record := AsyncMock()):
                    result = await search_conceptual_context("token question")
                    data = json.loads(result)

    assert data["results"][0]["summary"] == "REDACTED SUMMARY"
    record.assert_awaited_once()
    assert record.call_args.args[3] == "mcp.search_conceptual_context"
    assert record.call_args.kwargs["params"]["query"] == "token question"
    assert record.call_args.kwargs["record_ids"] == [ctx_id]
    assert record.call_args.kwargs["redacted"] == 1


async def test_view_active_decisions_without_redaction_records_zero_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_engine.begin.return_value = mock_context

    mock_store = AsyncMock()
    mock_store.get_org_global_top_contexts.return_value = [
        {
            "id": str(uuid.uuid4()),
            "summary": "Nothing sensitive here",
            "confidence_score": 0.9,
            "content_hash": "abc123",
            "last_verified_at": "2026-07-29T00:00:00+00:00",
            "sources": [],
        }
    ]

    record = AsyncMock()
    with _using(engine=mock_engine):
        with patch("checkcord.storage.postgres.PostgresEventStore", return_value=mock_store):
            with patch("checkcord.storage.audit.record", record):
                result = await view_active_decisions()
                data = json.loads(result)

    assert data["decisions"][0]["summary"] == "Nothing sensitive here"
    record.assert_awaited_once()
    assert record.call_args.kwargs["redacted"] == 0
    assert record.call_args.kwargs["result_count"] == 1


# --- team_identifier/path in the two retrieval MCP tools --------------------


async def test_search_conceptual_context_threads_team_identifier_and_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_engine.begin.return_value = mock_context

    mock_store = AsyncMock()
    mock_store.find_similar_contexts.return_value = []
    mock_embedding_client = AsyncMock()
    mock_embedding_client.embed_text.return_value = [0.1, 0.2, 0.3]

    with _using(engine=mock_engine):
        with patch("checkcord.storage.postgres.PostgresEventStore", return_value=mock_store):
            with _using(embedder=mock_embedding_client):
                await search_conceptual_context(
                    "Why did we choose Postgres?",
                    repo_identifier="acme/service-a",
                    team_identifier="backend",
                    path="src/auth.py",
                )

                mock_store.find_similar_contexts.assert_called_once_with(
                    uuid.UUID(tenant_id),
                    [0.1, 0.2, 0.3],
                    threshold=0.3,
                    repo_identifier="acme/service-a",
                    query_text="Why did we choose Postgres?",
                    team_identifier="backend",
                    path="src/auth.py",
                )


async def test_view_active_decisions_threads_team_identifier_and_path_no_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_engine.begin.return_value = mock_context

    mock_store = AsyncMock()
    mock_store.get_org_global_top_contexts.return_value = []

    with _using(engine=mock_engine):
        with patch("checkcord.storage.postgres.PostgresEventStore", return_value=mock_store):
            await view_active_decisions(team_identifier="backend", path="src/auth.py")

            mock_store.get_org_global_top_contexts.assert_called_once_with(
                uuid.UUID(tenant_id),
                limit=10,
                team_identifier="backend",
                path="src/auth.py",
                decisions_only=True,
            )


async def test_view_active_decisions_threads_team_identifier_and_path_with_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_engine.begin.return_value = mock_context

    mock_store = AsyncMock()
    mock_store.get_top_contexts_for_repo.return_value = []

    with _using(engine=mock_engine):
        with patch("checkcord.storage.postgres.PostgresEventStore", return_value=mock_store):
            await view_active_decisions(
                repo_identifier="acme/service-a", team_identifier="backend", path="src/auth.py"
            )

            mock_store.get_top_contexts_for_repo.assert_called_once_with(
                uuid.UUID(tenant_id),
                "acme/service-a",
                limit=10,
                team_identifier="backend",
                path="src/auth.py",
                decisions_only=True,
            )


# --- Usage-based personalization on the MCP surface -------------------------


async def _search_with_personalization(
    monkeypatch: pytest.MonkeyPatch,
    mock_store: AsyncMock,
    *,
    personalization_enabled: bool,
    resolved_user_id: uuid.UUID | None = None,
    repo_identifier: str | None = "acme/service-a",
) -> tuple[str, uuid.UUID]:
    tenant_id = str(uuid.uuid4())
    _call["tenant"] = uuid.UUID(str(tenant_id))
    monkeypatch.setenv("PERSONALIZATION_ENABLED", "true" if personalization_enabled else "false")
    get_settings.cache_clear()

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_engine.begin.return_value = mock_context

    mock_embedding_client = AsyncMock()
    mock_embedding_client.embed_text.return_value = [0.1, 0.2, 0.3]

    resolved = resolved_user_id if resolved_user_id is not None else ANONYMOUS_USER_ID

    with _using(engine=mock_engine):
        with _using(user_id=resolved):
            with patch("checkcord.storage.postgres.PostgresEventStore", return_value=mock_store):
                with _using(embedder=mock_embedding_client):
                    await search_conceptual_context(
                        "Why did we choose Postgres?", repo_identifier=repo_identifier
                    )

    return tenant_id, resolved


async def test_search_conceptual_context_personalization_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """personalization_enabled defaults to False: no affinity read or write
    calls at all.
    """
    mock_store = AsyncMock()
    mock_store.find_similar_contexts.return_value = [
        {"id": "ctx-1", "summary": "A decision", "confidence_score": 0.9, "similarity": 0.9}
    ]

    await _search_with_personalization(
        monkeypatch, mock_store, personalization_enabled=False, resolved_user_id=uuid.uuid4()
    )

    mock_store.record_user_repo_query.assert_not_called()
    mock_store.get_user_repo_affinity.assert_not_called()
    mock_store.get_org_global_ids.assert_not_called()


async def test_search_conceptual_context_personalization_skips_for_anonymous_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolved or anonymous identity never records or boosts, even with
    personalization_enabled=True.
    """
    mock_store = AsyncMock()
    mock_store.find_similar_contexts.return_value = [
        {"id": "ctx-1", "summary": "A decision", "confidence_score": 0.9, "similarity": 0.9}
    ]

    await _search_with_personalization(
        monkeypatch,
        mock_store,
        personalization_enabled=True,
        resolved_user_id=None,
    )

    mock_store.record_user_repo_query.assert_not_called()
    mock_store.get_user_repo_affinity.assert_not_called()
    mock_store.get_org_global_ids.assert_not_called()


async def test_search_conceptual_context_personalization_skips_without_repo_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The affinity signal only has meaning relative to a specific repo --
    an org-global-only query (no repo_identifier) never records/boosts."""
    mock_store = AsyncMock()
    mock_store.find_similar_contexts.return_value = [
        {"id": "ctx-1", "summary": "A decision", "confidence_score": 0.9, "similarity": 0.9}
    ]

    await _search_with_personalization(
        monkeypatch,
        mock_store,
        personalization_enabled=True,
        resolved_user_id=uuid.uuid4(),
        repo_identifier=None,
    )

    mock_store.record_user_repo_query.assert_not_called()
    mock_store.get_user_repo_affinity.assert_not_called()
    mock_store.get_org_global_ids.assert_not_called()


async def test_search_conceptual_context_personalization_enabled_boosts_and_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Personalization on, real user, repo given, prior affinity present:
    results are boosted and the query is recorded.
    """
    user_id = uuid.uuid4()
    mock_store = AsyncMock()
    mock_store.find_similar_contexts.return_value = [
        {"id": "org-1", "summary": "Org-wide rule", "confidence_score": 0.95, "similarity": 0.95},
        {"id": "repo-1", "summary": "Repo rule", "confidence_score": 0.9, "similarity": 0.9},
    ]
    mock_store.get_user_repo_affinity.return_value = 4
    mock_store.get_org_global_ids.return_value = {"org-1"}

    tenant_id, resolved_user_id = await _search_with_personalization(
        monkeypatch, mock_store, personalization_enabled=True, resolved_user_id=user_id
    )

    mock_store.get_user_repo_affinity.assert_called_once_with(
        uuid.UUID(tenant_id), resolved_user_id, "acme/service-a"
    )
    mock_store.get_org_global_ids.assert_called_once_with(uuid.UUID(tenant_id), ["org-1", "repo-1"])
    mock_store.record_user_repo_query.assert_called_once_with(
        uuid.UUID(tenant_id), resolved_user_id, "acme/service-a"
    )


async def test_search_conceptual_context_personalization_records_even_when_no_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty result set still tells us the user is working in this repo
    -- record_user_repo_query fires regardless, but boosting is skipped
    (nothing to reorder)."""
    user_id = uuid.uuid4()
    mock_store = AsyncMock()
    mock_store.find_similar_contexts.return_value = []

    tenant_id, resolved_user_id = await _search_with_personalization(
        monkeypatch, mock_store, personalization_enabled=True, resolved_user_id=user_id
    )

    mock_store.record_user_repo_query.assert_called_once_with(
        uuid.UUID(tenant_id), resolved_user_id, "acme/service-a"
    )
    mock_store.get_user_repo_affinity.assert_not_called()
    mock_store.get_org_global_ids.assert_not_called()


def _hit(n: int, similarity: float, context_type: str | None = None) -> dict[str, Any]:
    return {
        "id": str(uuid.UUID(int=n + 1)),
        "summary": f"r{n}",
        "similarity": similarity,
        "context_type": context_type,
    }


async def _search(rows: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    _call["tenant"] = uuid.uuid4()
    store = AsyncMock()
    store.find_similar_contexts.return_value = rows
    embedder = AsyncMock()
    embedder.embed_text.return_value = [0.1]
    with (
        _using(engine=MagicMock()),
        _using(embedder=embedder),
        patch("checkcord.storage.postgres.PostgresEventStore", return_value=store),
    ):
        return json.loads(await search_conceptual_context("money", **kwargs))


async def test_search_returns_ten_by_default_and_counts_every_match() -> None:
    data = await _search([_hit(n, 0.9 - n / 100) for n in range(44)])
    assert len(data["results"]) == 10 and data["total_matches"] == 44
    data = await _search([_hit(n, 0.9 - n / 100) for n in range(44)], limit=50)
    assert len(data["results"]) == 44 and data["total_matches"] == 44


@pytest.mark.parametrize("bad", [0, 51, True, "5"])
async def test_search_rejects_a_limit_out_of_range(bad: Any) -> None:
    assert "limit must be" in (await _search([], limit=bad))["error"]


async def test_a_decision_ranks_before_a_close_reference_record() -> None:
    rows = [
        _hit(1, 0.80),
        _hit(2, 0.78),
        _hit(3, 0.76, "adr"),
        _hit(4, 0.60),
        _hit(5, 0.50, "risk"),
    ]
    data = await _search(rows)
    assert [r["summary"] for r in data["results"]] == ["r3", "r1", "r2", "r4", "r5"]


async def test_search_uses_the_configured_minimum_similarity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from checkcord.core.config import get_settings

    monkeypatch.setenv("SEARCH_MIN_SIMILARITY", "0.55")
    get_settings.cache_clear()
    try:
        _call["tenant"] = uuid.uuid4()
        store = AsyncMock()
        store.find_similar_contexts.return_value = []
        embedder = AsyncMock()
        embedder.embed_text.return_value = [0.1]
        with (
            _using(engine=MagicMock()),
            _using(embedder=embedder),
            patch("checkcord.storage.postgres.PostgresEventStore", return_value=store),
        ):
            await search_conceptual_context("money")
        assert store.find_similar_contexts.call_args.kwargs["threshold"] == 0.55
    finally:
        get_settings.cache_clear()
