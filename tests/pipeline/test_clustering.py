from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.pipeline.clustering import ClusteringWorker
from checkcord.pipeline.reranker import RerankedCandidate
from checkcord.storage.postgres import PostgresEventStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
def tenant_id() -> Any:
    return uuid4()


@pytest.fixture
def make_event(tenant_id: Any) -> Any:
    def _make(**overrides: Any) -> CanonicalEvent:
        defaults = {
            "tenant_id": tenant_id,
            "source_platform": SourcePlatform.SLACK,
            "source_type": SourceType.MESSAGE,
            "source_id": "msg_123",
            "happened_at": "2026-06-20T12:00:00Z",
            "author": {"source_id": "user_456"},
            "content_raw": "hello",
            "content_clean": "hello",
            "access_control": {"is_public": True},
        }
        defaults.update(overrides)
        return CanonicalEvent(**defaults)

    return _make


async def test_clustering_worker_success(make_event: Any, tenant_id: Any) -> None:
    # Arrange
    event = make_event(metadata={"embedding": [0.1] * 1536})
    mock_bus = AsyncMock()
    mock_engine = MagicMock()

    # Mock the DB store inside the tenant context block
    mock_store = AsyncMock(spec=PostgresEventStore)
    mock_store.get_event_hold.return_value = (True, None)
    mock_store.find_similar_contexts.return_value = [
        {
            "id": uuid4(),
            "summary": "Matching Context 1",
            "confidence_score": 0.9,
            "similarity": 0.88,
        },
        {
            "id": uuid4(),
            "summary": "Matching Context 2",
            "confidence_score": 0.5,
            "similarity": 0.86,
        },
    ]

    mock_conn = MagicMock()
    # In checkcord.pipeline.clustering, it does:
    # async with tenant_context(self.engine, event.tenant_id) as conn:
    #     store = PostgresEventStore(conn)
    # So we patch the tenant_context and PostgresEventStore.
    from unittest.mock import patch

    with (
        patch("checkcord.pipeline.clustering.tenant_context") as mock_tenant_context,
        patch("checkcord.pipeline.clustering.PostgresEventStore", return_value=mock_store),
    ):
        # tenant_context returns a context manager that yields mock_conn
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_tenant_context.return_value = mock_ctx

        worker = ClusteringWorker(bus=mock_bus, engine=mock_engine, similarity_threshold=0.85)

        # Act
        await worker.process_message(event)

        # Assert
        # 1. find_similar_contexts was called
        mock_store.find_similar_contexts.assert_called_once_with(
            tenant_id=tenant_id,
            embedding=[0.1] * 1536,
            threshold=0.85,
            repo_identifier=None,
            include_sources=False,
            held_connection=None,
        )

        # 2. Match results were published
        mock_bus.publish.assert_called_once()
        published_topic, published_event = mock_bus.publish.call_args[0]
        assert published_topic == "events.linkage.pending"

        matches = published_event.metadata.get("clustering_matches")
        assert matches is not None
        assert len(matches) == 2
        assert matches[0]["summary"] == "Matching Context 1"
        assert matches[0]["similarity"] == 0.88
        assert matches[1]["summary"] == "Matching Context 2"
        assert matches[1]["similarity"] == 0.86


async def test_clustering_worker_passes_event_repo_for_scope_aware_dedup(
    make_event: Any, tenant_id: Any
) -> None:
    """Dedup search is scoped to the ingesting event's own repo.

    An event carrying code_metadata.repository must have that repo passed
    through to find_similar_contexts, so a repo-A insight only dedups
    against org-global or repo-A-scoped contexts, never a repo-B-scoped one.
    """
    # Arrange
    event = make_event(
        metadata={"embedding": [0.2] * 1536},
        code_metadata={"repository": "acme/service-a", "branch": "main"},
    )
    mock_bus = AsyncMock()
    mock_engine = MagicMock()

    mock_store = AsyncMock(spec=PostgresEventStore)
    mock_store.get_event_hold.return_value = (True, None)
    mock_store.find_similar_contexts.return_value = []

    mock_conn = MagicMock()
    from unittest.mock import patch

    with (
        patch("checkcord.pipeline.clustering.tenant_context") as mock_tenant_context,
        patch("checkcord.pipeline.clustering.PostgresEventStore", return_value=mock_store),
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_tenant_context.return_value = mock_ctx

        worker = ClusteringWorker(bus=mock_bus, engine=mock_engine, similarity_threshold=0.85)

        # Act
        await worker.process_message(event)

        # Assert
        mock_store.find_similar_contexts.assert_called_once_with(
            tenant_id=tenant_id,
            embedding=[0.2] * 1536,
            threshold=0.85,
            repo_identifier="acme/service-a",
            include_sources=False,
            held_connection=None,
        )


async def test_clustering_worker_no_embedding(make_event: Any) -> None:
    # Arrange
    event = make_event()  # No embedding in metadata
    mock_bus = AsyncMock()
    mock_engine = MagicMock()

    worker = ClusteringWorker(bus=mock_bus, engine=mock_engine)

    # Act
    await worker.process_message(event)

    # Assert
    mock_bus.publish.assert_not_called()


async def test_clustering_worker_error_propagation(make_event: Any, tenant_id: Any) -> None:
    # Arrange
    event = make_event(metadata={"embedding": [0.1] * 1536})
    mock_bus = AsyncMock()
    mock_engine = MagicMock()

    mock_store = AsyncMock(spec=PostgresEventStore)
    mock_store.get_event_hold.return_value = (True, None)
    mock_store.find_similar_contexts.side_effect = RuntimeError("Database timeout")

    mock_conn = MagicMock()
    from unittest.mock import patch

    with (
        patch("checkcord.pipeline.clustering.tenant_context") as mock_tenant_context,
        patch("checkcord.pipeline.clustering.PostgresEventStore", return_value=mock_store),
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_tenant_context.return_value = mock_ctx

        worker = ClusteringWorker(bus=mock_bus, engine=mock_engine)

        # Act & Assert
        with pytest.raises(RuntimeError, match="Database timeout"):
            await worker.process_message(event)

        mock_bus.publish.assert_not_called()


# ---------------------------------------------------------------------------
# Reranking.
# ---------------------------------------------------------------------------


async def test_clustering_worker_reranks_candidates_when_enabled(
    make_event: Any, tenant_id: Any
) -> None:
    """A reranker that flips the DB's cosine-distance order must be reflected
    in the published clustering_matches order: it decides which candidate
    LinkageWorker's first-positive-match loop sees first.
    """
    ctx_first_by_cosine = uuid4()
    ctx_second_by_cosine = uuid4()
    event = make_event(metadata={"embedding": [0.1] * 1536})
    mock_bus = AsyncMock()
    mock_engine = MagicMock()

    mock_store = AsyncMock(spec=PostgresEventStore)
    mock_store.get_event_hold.return_value = (True, None)
    mock_store.find_similar_contexts.return_value = [
        {
            "id": ctx_first_by_cosine,
            "summary": "Closer by cosine, but less relevant",
            "confidence_score": 0.5,
            "similarity": 0.90,
        },
        {
            "id": ctx_second_by_cosine,
            "summary": "Farther by cosine, but the reranker prefers it",
            "confidence_score": 0.5,
            "similarity": 0.86,
        },
    ]

    mock_reranker = AsyncMock()
    mock_reranker.rerank.return_value = [
        RerankedCandidate(id=str(ctx_second_by_cosine), relevance_score=0.99),
        RerankedCandidate(id=str(ctx_first_by_cosine), relevance_score=0.10),
    ]

    mock_conn = MagicMock()
    with (
        patch("checkcord.pipeline.clustering.tenant_context") as mock_tenant_context,
        patch("checkcord.pipeline.clustering.PostgresEventStore", return_value=mock_store),
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_tenant_context.return_value = mock_ctx

        worker = ClusteringWorker(
            bus=mock_bus, engine=mock_engine, similarity_threshold=0.85, reranker=mock_reranker
        )
        worker.settings.rerank_candidate_pool_size = 20

        await worker.process_message(event)

    # The candidate-pool cap only applies when a reranker is active.
    mock_store.find_similar_contexts.assert_called_once_with(
        tenant_id=tenant_id,
        embedding=[0.1] * 1536,
        threshold=0.85,
        repo_identifier=None,
        include_sources=False,
        limit=20,
        held_connection=None,
    )
    mock_reranker.rerank.assert_awaited_once()

    published_topic, published_event = mock_bus.publish.call_args[0]
    assert published_topic == "events.linkage.pending"
    matches = published_event.metadata["clustering_matches"]
    assert [m["context_id"] for m in matches] == [
        str(ctx_second_by_cosine),
        str(ctx_first_by_cosine),
    ]


async def test_clustering_worker_rerank_failure_falls_back_to_original_order(
    make_event: Any, tenant_id: Any
) -> None:
    """A reranker exception must never fail the event: matches are published
    in the pre-rerank (cosine-distance) order instead.
    """
    event = make_event(metadata={"embedding": [0.1] * 1536})
    mock_bus = AsyncMock()
    mock_engine = MagicMock()

    mock_store = AsyncMock(spec=PostgresEventStore)
    mock_store.get_event_hold.return_value = (True, None)
    mock_store.find_similar_contexts.return_value = [
        {"id": uuid4(), "summary": "Match 1", "confidence_score": 0.5, "similarity": 0.90},
        {"id": uuid4(), "summary": "Match 2", "confidence_score": 0.5, "similarity": 0.86},
    ]

    mock_reranker = AsyncMock()
    mock_reranker.rerank.side_effect = TimeoutError("voyage rerank timed out")

    mock_conn = MagicMock()
    with (
        patch("checkcord.pipeline.clustering.tenant_context") as mock_tenant_context,
        patch("checkcord.pipeline.clustering.PostgresEventStore", return_value=mock_store),
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_tenant_context.return_value = mock_ctx

        worker = ClusteringWorker(
            bus=mock_bus, engine=mock_engine, similarity_threshold=0.85, reranker=mock_reranker
        )

        # Must not raise -- the whole point of M2's fallback.
        await worker.process_message(event)

    mock_bus.publish.assert_called_once()
    _topic, published_event = mock_bus.publish.call_args[0]
    matches = published_event.metadata["clustering_matches"]
    assert [m["summary"] for m in matches] == ["Match 1", "Match 2"]


async def test_clustering_worker_reranker_not_called_when_no_candidates(
    make_event: Any, tenant_id: Any
) -> None:
    """No candidates: the reranker is not invoked."""
    event = make_event(metadata={"embedding": [0.1] * 1536})
    mock_bus = AsyncMock()
    mock_engine = MagicMock()

    mock_store = AsyncMock(spec=PostgresEventStore)
    mock_store.get_event_hold.return_value = (True, None)
    mock_store.find_similar_contexts.return_value = []

    mock_reranker = AsyncMock()

    mock_conn = MagicMock()
    with (
        patch("checkcord.pipeline.clustering.tenant_context") as mock_tenant_context,
        patch("checkcord.pipeline.clustering.PostgresEventStore", return_value=mock_store),
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_tenant_context.return_value = mock_ctx

        worker = ClusteringWorker(
            bus=mock_bus, engine=mock_engine, similarity_threshold=0.85, reranker=mock_reranker
        )
        await worker.process_message(event)

    mock_reranker.rerank.assert_not_called()
    mock_bus.publish.assert_called_once()


async def test_clustering_worker_defaults_to_no_reranker_when_disabled_in_settings(
    tenant_id: Any,
) -> None:
    """Without an explicit `reranker=` override, construction reads settings
    and resolves to None when rerank_enabled=False (the default)."""
    mock_bus = AsyncMock()
    mock_engine = MagicMock()
    worker = ClusteringWorker(bus=mock_bus, engine=mock_engine)
    assert worker.reranker is None
