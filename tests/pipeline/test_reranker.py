from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from checkcord.core.config import Settings
from checkcord.pipeline.reranker import (
    RerankCandidate,
    VoyageReranker,
    build_reranker,
)


@dataclass
class _FakeRerankResultItem:
    index: int
    relevance_score: float


@dataclass
class _FakeRerankingObject:
    results: list[_FakeRerankResultItem]


def _candidates() -> list[RerankCandidate]:
    return [
        RerankCandidate(id="ctx-a", text="We decided to migrate to Kafka for replayability."),
        RerankCandidate(id="ctx-b", text="Unrelated context about CSS overflow."),
        RerankCandidate(id="ctx-c", text="Redis eviction policy changed to allkeys-lru."),
    ]


@pytest.mark.asyncio
async def test_voyage_reranker_reorders_by_relevance_score() -> None:
    """Voyage returns results best-first by its own index into `documents` --
    the reranker must map that back to candidate ids, not positions."""
    with patch("voyageai.client_async.AsyncClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.rerank = AsyncMock(
            return_value=_FakeRerankingObject(
                results=[
                    _FakeRerankResultItem(index=2, relevance_score=0.95),  # ctx-c
                    _FakeRerankResultItem(index=0, relevance_score=0.40),  # ctx-a
                    _FakeRerankResultItem(index=1, relevance_score=0.01),  # ctx-b
                ]
            )
        )

        reranker = VoyageReranker(api_key="test-key", model="rerank-2.5", timeout_seconds=10.0)
        result = await reranker.rerank(query="redis config change", candidates=_candidates())

    assert [r.id for r in result] == ["ctx-c", "ctx-a", "ctx-b"]
    assert result[0].relevance_score == 0.95
    mock_client.rerank.assert_awaited_once_with(
        query="redis config change",
        documents=[c.text for c in _candidates()],
        model="rerank-2.5",
    )


@pytest.mark.asyncio
async def test_voyage_reranker_empty_candidates_short_circuits_without_api_call() -> None:
    with patch("voyageai.client_async.AsyncClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.rerank = AsyncMock()

        reranker = VoyageReranker(api_key="test-key", model="rerank-2.5", timeout_seconds=10.0)
        result = await reranker.rerank(query="anything", candidates=[])

    assert result == []
    mock_client.rerank.assert_not_awaited()


@pytest.mark.asyncio
async def test_voyage_reranker_propagates_failure() -> None:
    """VoyageReranker either returns a valid reordering or raises; falling
    back is ClusteringWorker's job.
    """
    with patch("voyageai.client_async.AsyncClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.rerank = AsyncMock(side_effect=TimeoutError("voyage rerank timed out"))

        reranker = VoyageReranker(api_key="test-key", model="rerank-2.5", timeout_seconds=10.0)
        with pytest.raises(TimeoutError, match="voyage rerank timed out"):
            await reranker.rerank(query="anything", candidates=_candidates())


@pytest.mark.asyncio
async def test_voyage_reranker_constructs_client_with_bounded_retries_and_timeout() -> None:
    """max_retries and timeout are passed through to voyageai.AsyncClient."""
    with patch("voyageai.client_async.AsyncClient") as mock_client_cls:
        VoyageReranker(api_key="test-key", model="rerank-2.5", timeout_seconds=7.5)

    mock_client_cls.assert_called_once_with(api_key="test-key", max_retries=3, timeout=7.5)


def test_build_reranker_disabled_returns_none() -> None:
    settings = Settings(rerank_enabled=False, voyage_api_key="voyage-test-key")
    assert build_reranker(settings) is None


def test_build_reranker_enabled_but_no_api_key_returns_none() -> None:
    settings = Settings(rerank_enabled=True, voyage_api_key=None)
    assert build_reranker(settings) is None


def test_build_reranker_enabled_with_api_key_returns_voyage_reranker() -> None:
    settings = Settings(
        rerank_enabled=True,
        voyage_api_key="voyage-test-key",
        rerank_model="rerank-2.5",
        rerank_timeout_seconds=10.0,
    )
    with patch("voyageai.client_async.AsyncClient"):
        reranker = build_reranker(settings)
    assert isinstance(reranker, VoyageReranker)
