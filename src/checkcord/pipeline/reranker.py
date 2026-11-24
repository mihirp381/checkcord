"""Swappable reranking backend for the merge/cluster stage.

`ClusteringWorker` talks only to the `Reranker` Protocol, never to
`VoyageReranker` directly.
"""

from __future__ import annotations

from typing import Protocol

import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__).bind(component="reranker")


class RerankCandidate(BaseModel):
    """One candidate handed to a `Reranker` -- an opaque id plus the text to score."""

    id: str
    text: str


class RerankedCandidate(BaseModel):
    """One scored result. `id` matches a `RerankCandidate.id` from the input list."""

    id: str
    relevance_score: float


class Reranker(Protocol):
    """Structural interface every reranker backend satisfies."""

    async def rerank(
        self, query: str, candidates: list[RerankCandidate]
    ) -> list[RerankedCandidate]:
        """Return `candidates` reordered best-first by relevance to `query`.

        Implementations may return fewer results than were given but must never
        return an id that was not in `candidates`. Raises on failure; callers
        (see `pipeline/clustering.py`) catch and fall back.
        """
        ...


class RerankerSettings(Protocol):
    """Structural type for the config fields this factory needs.

    Read-only properties, not plain attributes -- see
    `embeddings_factory.EmbeddingSettings` for why (this factory only ever
    reads these fields, never assigns to them).
    """

    @property
    def rerank_enabled(self) -> bool: ...
    @property
    def rerank_model(self) -> str: ...
    @property
    def rerank_timeout_seconds(self) -> float: ...
    @property
    def voyage_api_key(self) -> str | None: ...


class VoyageReranker:
    """Reranker backed by Voyage AI's rerank API.

    Retries and timeouts are handled by `voyageai.AsyncClient`'s own
    `max_retries` and `timeout` settings.
    """

    _MAX_ATTEMPTS = 3

    def __init__(self, api_key: str, model: str, timeout_seconds: float) -> None:
        import voyageai

        self._client = voyageai.client_async.AsyncClient(
            api_key=api_key,
            max_retries=self._MAX_ATTEMPTS,
            timeout=timeout_seconds,
        )
        self._model = model

    async def rerank(
        self, query: str, candidates: list[RerankCandidate]
    ) -> list[RerankedCandidate]:
        if not candidates:
            return []

        from checkcord.llm import usage

        with usage.timed(usage.RERANK, "rerank", self._model) as tokens:
            result = await self._client.rerank(
                query=query,
                documents=[c.text for c in candidates],
                model=self._model,
            )
            tokens["input_tokens"] = getattr(result, "total_tokens", None)
        return [
            RerankedCandidate(
                id=candidates[item.index].id,
                relevance_score=item.relevance_score,
            )
            for item in result.results
        ]


def build_reranker(settings: RerankerSettings) -> Reranker | None:
    """Construct the configured `Reranker`, or `None` if disabled or unconfigured.

    Callers treat `None` as reranking being off, not as an error.
    """
    if not settings.rerank_enabled:
        return None
    if not settings.voyage_api_key:
        logger.warning("reranker.disabled_no_api_key")
        return None

    logger.info("reranker.initializing_voyage", model=settings.rerank_model)
    return VoyageReranker(
        api_key=settings.voyage_api_key,
        model=settings.rerank_model,
        timeout_seconds=settings.rerank_timeout_seconds,
    )
