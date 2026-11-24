import pytest
from langchain_core.embeddings import Embeddings

from checkcord.pipeline.embedder import EmbeddingClient


@pytest.mark.asyncio
async def test_embed_text_normal(dummy_embeddings: Embeddings) -> None:
    client = EmbeddingClient(dummy_embeddings)
    embedding = await client.embed_text("Some normal text")
    assert embedding is not None
    assert len(embedding) == 1536
    assert all(x == 0.1 for x in embedding)


@pytest.mark.asyncio
async def test_embed_text_empty(dummy_embeddings: Embeddings) -> None:
    client = EmbeddingClient(dummy_embeddings)
    embedding = await client.embed_text("   ")
    assert embedding is None


@pytest.mark.asyncio
async def test_embed_text_truncation(dummy_embeddings: Embeddings) -> None:
    # 35000 characters
    long_text = "a" * 35000

    # We will verify that it doesn't crash and returns the correct mocked embedding
    # We can mock the embeddings to assert that the received text is truncated to 30000
    class VerifyTruncationEmbeddings(Embeddings):
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return []

        def embed_query(self, text: str) -> list[float]:
            return []

        async def aembed_query(self, text: str) -> list[float]:
            assert len(text) == 30000
            return [0.5] * 1536

        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            return []

    truncation_client = EmbeddingClient(VerifyTruncationEmbeddings())
    embedding = await truncation_client.embed_text(long_text)
    assert embedding is not None
    assert len(embedding) == 1536
    assert all(x == 0.5 for x in embedding)


@pytest.mark.asyncio
async def test_embed_text_error() -> None:
    class FailingEmbeddings(Embeddings):
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return []

        def embed_query(self, text: str) -> list[float]:
            return []

        async def aembed_query(self, text: str) -> list[float]:
            raise ValueError("API error")

        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            return []

    failing_client = EmbeddingClient(FailingEmbeddings())
    with pytest.raises(ValueError, match="API error"):
        await failing_client.embed_text("Raise error")


@pytest.mark.asyncio
async def test_embed_texts_batch_normal(dummy_embeddings: Embeddings) -> None:
    client = EmbeddingClient(dummy_embeddings)
    results = await client.embed_texts(["first text", "second text", "third text"])
    assert len(results) == 3
    assert all(r is not None and len(r) == 1536 for r in results)


@pytest.mark.asyncio
async def test_embed_texts_batch_preserves_order_and_length() -> None:
    """The batch result list must line up 1:1 with the input list, including
    empty slots -- callers (e.g. the reembed backfill script) zip results
    against the original rows and must never see them shifted."""

    class OrderTrackingEmbeddings(Embeddings):
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return []

        def embed_query(self, text: str) -> list[float]:
            return []

        async def aembed_query(self, text: str) -> list[float]:
            return []

        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            # Distinct, position-derived vectors so the test can verify
            # each non-empty input landed in its own original slot.
            return [[float(len(t))] for t in texts]

    client = EmbeddingClient(OrderTrackingEmbeddings())
    results = await client.embed_texts(["aa", "", "bbbb", "   ", "c"])
    assert results == [[2.0], None, [4.0], None, [1.0]]


@pytest.mark.asyncio
async def test_embed_texts_batch_all_empty_skips_api_call() -> None:
    class AssertNotCalledEmbeddings(Embeddings):
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return []

        def embed_query(self, text: str) -> list[float]:
            return []

        async def aembed_query(self, text: str) -> list[float]:
            return []

        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            raise AssertionError("aembed_documents must not be called for an all-empty batch")

    client = EmbeddingClient(AssertNotCalledEmbeddings())
    results = await client.embed_texts(["", "   ", ""])
    assert results == [None, None, None]


@pytest.mark.asyncio
async def test_embed_texts_batch_truncation(dummy_embeddings: Embeddings) -> None:
    long_text = "a" * 35000

    class VerifyBatchTruncationEmbeddings(Embeddings):
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return []

        def embed_query(self, text: str) -> list[float]:
            return []

        async def aembed_query(self, text: str) -> list[float]:
            return []

        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            assert all(len(t) == 30000 for t in texts)
            return [[0.5] * 1536 for _ in texts]

    client = EmbeddingClient(VerifyBatchTruncationEmbeddings())
    results = await client.embed_texts([long_text, long_text])
    assert len(results) == 2
    assert all(r is not None and len(r) == 1536 for r in results)


@pytest.mark.asyncio
async def test_embed_texts_batch_error() -> None:
    class FailingBatchEmbeddings(Embeddings):
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return []

        def embed_query(self, text: str) -> list[float]:
            return []

        async def aembed_query(self, text: str) -> list[float]:
            return []

        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            raise ValueError("batch API error")

    client = EmbeddingClient(FailingBatchEmbeddings())
    with pytest.raises(ValueError, match="batch API error"):
        await client.embed_texts(["one", "two"])
