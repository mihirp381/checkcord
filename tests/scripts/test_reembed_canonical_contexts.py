import uuid
from unittest.mock import AsyncMock

import pytest
from langchain_core.embeddings import Embeddings

from checkcord.pipeline.embedder import EmbeddingClient
from scripts.reembed_canonical_contexts import (
    BackfillResult,
    _embed_batch_with_retry,
    run_backfill,
)

pytestmark = pytest.mark.asyncio


class _FakeContextStore:
    """In-memory stand-in for the two PostgresEventStore methods the script
    needs, driven by a plain dict so tests can assert on exactly what got
    written without a real database."""

    def __init__(self, rows: list[tuple[uuid.UUID, uuid.UUID, str]]) -> None:
        # id -> (tenant_id, summary, embedding-or-None)
        self._rows: dict[uuid.UUID, tuple[uuid.UUID, str, list[float] | None]] = {
            row_id: (tenant_id, summary, None) for row_id, tenant_id, summary in rows
        }
        self.update_calls: list[list[tuple[uuid.UUID, uuid.UUID, list[float]]]] = []

    async def get_canonical_contexts_missing_embedding(
        self, after_id: uuid.UUID | None, limit: int
    ) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
        candidates = sorted(
            (
                (row_id, tenant_id, summary)
                for row_id, (tenant_id, summary, embedding) in self._rows.items()
                if embedding is None
            ),
            key=lambda r: r[0].int,
        )
        if after_id is not None:
            candidates = [r for r in candidates if r[0].int > after_id.int]
        return candidates[:limit]

    async def update_canonical_context_embeddings_batch(
        self, updates: list[tuple[uuid.UUID, uuid.UUID, list[float]]]
    ) -> int:
        self.update_calls.append(updates)
        for tenant_id, row_id, embedding in updates:
            _, summary, _ = self._rows[row_id]
            self._rows[row_id] = (tenant_id, summary, embedding)
        return len(updates)


def _make_rows(
    n: int, tenant_id: uuid.UUID | None = None
) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    tenant_id = tenant_id or uuid.uuid4()
    # uuid1 is monotonic-ish by insertion order under sequential calls in
    # CPython, but to keep test ordering fully deterministic we build
    # explicit sequential UUIDs instead of relying on that.
    return [(uuid.UUID(int=i + 1), tenant_id, f"summary {i}") for i in range(n)]


class _DummyEmbeddings(Embeddings):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4 for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return [0.1] * 4

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4 for _ in texts]

    async def aembed_query(self, text: str) -> list[float]:
        return [0.1] * 4


async def test_run_backfill_embeds_all_rows_in_batches() -> None:
    rows = _make_rows(5)
    store = _FakeContextStore(rows)
    embedder = EmbeddingClient(_DummyEmbeddings())

    result = await run_backfill(store, embedder, batch_size=2)

    assert result.success is True
    assert result.processed == 5
    assert result.updated == 5
    assert result.skipped_empty == 0
    # 5 rows at batch_size=2 -> batches of 2, 2, 1
    assert [len(call) for call in store.update_calls] == [2, 2, 1]
    assert all(embedding is not None for _, _, embedding in store._rows.values())


async def test_run_backfill_idempotent_skips_already_embedded_rows() -> None:
    """Re-running against a store where some rows already have an embedding
    must not re-embed them.
    """
    rows = _make_rows(4)
    store = _FakeContextStore(rows)
    embedder = EmbeddingClient(_DummyEmbeddings())

    first = await run_backfill(store, embedder, batch_size=10)
    assert first.updated == 4

    # Second run against the same (now fully-embedded) store: nothing left
    # to do, must not call the embedder or the update method at all.
    embedder_spy = EmbeddingClient(_DummyEmbeddings())
    embedder_spy.embeddings.aembed_documents = AsyncMock(
        side_effect=AssertionError("must not be called on a fully-embedded store")
    )
    second = await run_backfill(store, embedder_spy, batch_size=10)

    assert second.success is True
    assert second.processed == 0
    assert second.updated == 0


async def test_run_backfill_partial_completion_then_resume() -> None:
    """Simulates a crash after the first batch: a fresh run against the same
    store only processes the rows still missing an embedding."""
    rows = _make_rows(6)
    store = _FakeContextStore(rows)
    embedder = EmbeddingClient(_DummyEmbeddings())

    # First "run": only ever fetch and complete a single batch, as if the
    # process died right after committing it.
    first_batch = await store.get_canonical_contexts_missing_embedding(None, 2)
    embeddings = await embedder.embed_texts([s for _, _, s in first_batch])
    updates = [
        (tenant_id, row_id, emb)
        for (row_id, tenant_id, _summary), emb in zip(first_batch, embeddings, strict=True)
        if emb is not None
    ]
    await store.update_canonical_context_embeddings_batch(updates)
    assert len(store.update_calls) == 1

    # "Resume": a fresh run_backfill call, cursor reset to the beginning,
    # must pick up exactly the remaining 4 rows -- not re-touch the first 2.
    result = await run_backfill(store, embedder, batch_size=2)
    assert result.processed == 4
    assert result.updated == 4
    assert all(embedding is not None for _, _, embedding in store._rows.values())


async def test_run_backfill_empty_summary_left_null_not_retried() -> None:
    """A row whose summary embeds to None (empty/whitespace) stays NULL and is
    neither treated as a failure nor looped on forever.
    """
    tenant_id = uuid.uuid4()
    rows = [
        (uuid.UUID(int=1), tenant_id, "real content"),
        (uuid.UUID(int=2), tenant_id, "   "),
        (uuid.UUID(int=3), tenant_id, "more real content"),
    ]
    store = _FakeContextStore(rows)
    embedder = EmbeddingClient(_DummyEmbeddings())

    result = await run_backfill(store, embedder, batch_size=10)

    assert result.success is True
    assert result.processed == 3
    assert result.updated == 2
    assert result.skipped_empty == 1
    empty_row_id = uuid.UUID(int=2)
    assert store._rows[empty_row_id][2] is None


async def test_run_backfill_no_rows_is_a_no_op() -> None:
    store = _FakeContextStore([])
    embedder = EmbeddingClient(_DummyEmbeddings())
    result = await run_backfill(store, embedder, batch_size=10)
    assert result == BackfillResult(success=True, processed=0, updated=0, skipped_empty=0)


async def test_run_backfill_permanent_failure_returns_unsuccessful_result() -> None:
    """An unrecoverable embedding failure surfaces as a failed result (the
    script maps it to a non-zero exit code); it neither raises past the
    caller nor drops rows silently.
    """
    rows = _make_rows(2)
    store = _FakeContextStore(rows)

    class AlwaysFailingEmbeddings(Embeddings):
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return []

        def embed_query(self, text: str) -> list[float]:
            return []

        async def aembed_query(self, text: str) -> list[float]:
            return []

        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("Voyage API permanently unavailable")

    embedder = EmbeddingClient(AlwaysFailingEmbeddings())
    result = await run_backfill(store, embedder, batch_size=10, max_retries=1, backoff_seconds=0)

    assert result.success is False
    assert result.processed == 0
    assert result.updated == 0
    assert "Voyage API permanently unavailable" in (result.error or "")


async def test_embed_batch_with_retry_recovers_from_transient_rate_limit() -> None:
    """A Voyage rate-limit error is retried with backoff and does not abort the run."""
    call_count = 0

    class FlakyThenSucceedsEmbeddings(Embeddings):
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return []

        def embed_query(self, text: str) -> list[float]:
            return []

        async def aembed_query(self, text: str) -> list[float]:
            return []

        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("429 rate limit exceeded")
            return [[0.1] * 4 for _ in texts]

    embedder = EmbeddingClient(FlakyThenSucceedsEmbeddings())
    results = await _embed_batch_with_retry(embedder, ["a", "b"], max_retries=5, backoff_seconds=0)

    assert call_count == 3
    assert results == [[0.1] * 4, [0.1] * 4]


async def test_embed_batch_with_retry_exhausts_and_raises() -> None:
    class AlwaysRateLimitedEmbeddings(Embeddings):
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return []

        def embed_query(self, text: str) -> list[float]:
            return []

        async def aembed_query(self, text: str) -> list[float]:
            return []

        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("429 rate limit exceeded")

    embedder = EmbeddingClient(AlwaysRateLimitedEmbeddings())
    with pytest.raises(RuntimeError, match="429 rate limit exceeded"):
        await _embed_batch_with_retry(embedder, ["a"], max_retries=3, backoff_seconds=0)
