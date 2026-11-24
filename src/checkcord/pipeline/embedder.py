from __future__ import annotations

import structlog
from langchain_core.embeddings import Embeddings

from checkcord.llm import usage

logger = structlog.get_logger(__name__)


class EmbeddingDimensionError(ValueError):
    pass


class EmbeddingClient:
    """Embeds text through a LangChain embeddings provider."""

    def __init__(
        self, embeddings: Embeddings, purpose: str = "embedding", dimension: int | None = None
    ) -> None:
        self.embeddings = embeddings
        self.purpose = purpose
        self.dimension = dimension
        self.model = str(getattr(embeddings, "model", None) or type(embeddings).__name__)

    def _checked(self, embedding: list[float]) -> list[float]:
        if self.dimension is not None and len(embedding) != self.dimension:
            raise EmbeddingDimensionError(
                f"{self.model} returned {len(embedding)}-wide vectors but EMBEDDING_DIMENSION "
                f"is {self.dimension}; set EMBEDDING_DIMENSION={len(embedding)} and run "
                "`checkcord migrate`."
            )
        return embedding

    async def embed_text(self, text: str) -> list[float] | None:
        """Generate a dense embedding for the given text.

        Args:
            text: The text string to embed.

        Returns:
            A list of floats representing the embedding, or None if the text is empty.
        """
        sanitized = text.strip()
        if not sanitized:
            logger.debug("embedder.skip_empty_text")
            return None

        # Truncate content to ~30k characters to prevent token limit errors
        if len(sanitized) > 30000:
            logger.info("embedder.truncating_content", original_len=len(sanitized))
            sanitized = sanitized[:30000]

        try:
            # LangChain Embeddings interface has aembed_query
            with usage.timed(usage.EMBEDDING, self.purpose, self.model):
                embedding = await self.embeddings.aembed_query(sanitized)
            if isinstance(embedding, list):
                return self._checked([float(x) for x in embedding])
            return None
        except Exception as e:
            logger.exception("embedder.error", error=str(e))
            raise

    async def embed_texts(self, texts: list[str]) -> list[list[float] | None]:
        """Generate dense embeddings for a batch of texts in one provider call.

        Same sanitize/truncate rules as embed_text (empty/whitespace-only
        text -> None in that slot, no API call for it; >30k chars ->
        truncated), applied per-item, so a bulk caller (e.g.
        scripts/reembed_canonical_contexts.py) can never silently diverge
        from live ingestion's per-item truncation behavior. Returns a list
        the same length and order as ``texts``; non-empty entries are
        embedded together via a single ``aembed_documents`` call, which is
        the batch counterpart of embed_text's ``aembed_query``.
        """
        sanitized_by_index: dict[int, str] = {}
        for i, text in enumerate(texts):
            sanitized = text.strip()
            if not sanitized:
                continue
            if len(sanitized) > 30000:
                logger.info("embedder.truncating_content", original_len=len(sanitized))
                sanitized = sanitized[:30000]
            sanitized_by_index[i] = sanitized

        if not sanitized_by_index:
            logger.debug("embedder.skip_empty_batch")
            return [None] * len(texts)

        indices = list(sanitized_by_index.keys())
        try:
            with usage.timed(usage.EMBEDDING, self.purpose, self.model):
                embeddings = await self.embeddings.aembed_documents(
                    [sanitized_by_index[i] for i in indices]
                )
        except Exception as e:
            logger.exception("embedder.batch_error", error=str(e), batch_size=len(indices))
            raise

        results: list[list[float] | None] = [None] * len(texts)
        for i, embedding in zip(indices, embeddings, strict=True):
            if isinstance(embedding, list):
                results[i] = self._checked([float(x) for x in embedding])
        return results
