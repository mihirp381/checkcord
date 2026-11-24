#!/usr/bin/env python3
"""Re-embed every canonical_contexts row with a NULL embedding.

Run by hand after changing the embedding model, once `checkcord migrate
--reset-embeddings` has cleared the column.

Resumable: a re-run only embeds rows still holding a NULL embedding, so no
row is embedded (or billed) twice. Embedding calls are batched.

Usage:
    uv run python scripts/reembed_canonical_contexts.py [--batch-size N]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from checkcord.core.config import get_settings
from checkcord.pipeline.embedder import EmbeddingClient
from checkcord.pipeline.embeddings_factory import build_embeddings_provider
from checkcord.storage.postgres import (
    PostgresEventStore,
    create_checkcord_engine,
    list_tenant_ids,
    tenant_context,
)

logger = logging.getLogger("reembed_canonical_contexts")

DEFAULT_BATCH_SIZE = 50
DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_SECONDS = 2.0


class ContextStore(Protocol):
    """The two PostgresEventStore methods this script needs -- narrowed for testability."""

    async def get_canonical_contexts_missing_embedding(
        self, after_id: UUID | None, limit: int
    ) -> list[tuple[UUID, UUID, str]]: ...

    async def update_canonical_context_embeddings_batch(
        self, updates: list[tuple[UUID, UUID, list[float]]]
    ) -> int: ...


@dataclass
class BackfillResult:
    success: bool
    processed: int = 0
    updated: int = 0
    skipped_empty: int = 0
    error: str | None = None


async def _embed_batch_with_retry(
    embedder: EmbeddingClient,
    texts: list[str],
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
) -> list[list[float] | None]:
    """Call embed_texts with exponential backoff on transient (e.g. rate-limit) errors."""
    attempt = 1
    delay = backoff_seconds
    while True:
        try:
            return await embedder.embed_texts(texts)
        except Exception as e:
            if attempt >= max_retries:
                logger.error("batch embedding failed permanently after %d attempts: %s", attempt, e)
                raise
            logger.warning(
                "batch embedding failed (attempt %d/%d): %s -- retrying in %.1fs",
                attempt,
                max_retries,
                e,
                delay,
            )
            if delay > 0:
                await asyncio.sleep(delay)
            delay *= 2
            attempt += 1


async def run_backfill(
    store: ContextStore,
    embedder: EmbeddingClient,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
) -> BackfillResult:
    """Page through NULL-embedding rows and re-embed them, batch by batch.

    Rows with an empty summary (embed_texts returns None for that slot) are
    left NULL and excluded from the UPDATE.
    """
    total_processed = 0
    total_updated = 0
    total_skipped_empty = 0
    cursor_id: UUID | None = None

    while True:
        rows = await store.get_canonical_contexts_missing_embedding(cursor_id, batch_size)
        if not rows:
            break

        texts = [summary for (_row_id, _tenant_id, summary) in rows]
        try:
            embeddings = await _embed_batch_with_retry(
                embedder, texts, max_retries=max_retries, backoff_seconds=backoff_seconds
            )
        except Exception as e:
            return BackfillResult(
                success=False,
                processed=total_processed,
                updated=total_updated,
                skipped_empty=total_skipped_empty,
                error=str(e),
            )

        updates = [
            (tenant_id, row_id, embedding)
            for (row_id, tenant_id, _summary), embedding in zip(rows, embeddings, strict=True)
            if embedding is not None
        ]
        if updates:
            await store.update_canonical_context_embeddings_batch(updates)

        total_processed += len(rows)
        total_updated += len(updates)
        total_skipped_empty += len(rows) - len(updates)
        cursor_id = rows[-1][0]

        logger.info(
            "backfill progress: processed=%d updated=%d skipped_empty=%d",
            total_processed,
            total_updated,
            total_skipped_empty,
        )

    return BackfillResult(
        success=True,
        processed=total_processed,
        updated=total_updated,
        skipped_empty=total_skipped_empty,
    )


async def _main_async(batch_size: int) -> int:
    settings = get_settings()
    provider = build_embeddings_provider(settings)
    if provider is None:
        logger.error(
            "No embedding provider configured -- missing API key for embedding_provider=%r",
            settings.embedding_provider,
        )
        return 1
    embedder = EmbeddingClient(provider, dimension=settings.embedding_dimension)

    engine = create_checkcord_engine(settings.database_url)
    result = BackfillResult(success=True, processed=0, updated=0, skipped_empty=0)
    try:
        # Row-level security limits each pass to one tenant.
        for tenant_id in await list_tenant_ids(engine):
            async with tenant_context(engine, tenant_id) as conn:
                part = await run_backfill(PostgresEventStore(conn), embedder, batch_size=batch_size)
            result = BackfillResult(
                success=part.success,
                processed=result.processed + part.processed,
                updated=result.updated + part.updated,
                skipped_empty=result.skipped_empty + part.skipped_empty,
                error=part.error,
            )
            if not part.success:
                break
    finally:
        await engine.dispose()

    if not result.success:
        logger.error(
            "backfill aborted: processed=%d updated=%d skipped_empty=%d error=%s",
            result.processed,
            result.updated,
            result.skipped_empty,
            result.error,
        )
        return 1

    logger.info(
        "backfill complete: processed=%d updated=%d skipped_empty=%d",
        result.processed,
        result.updated,
        result.skipped_empty,
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Re-embed every canonical_contexts row with a NULL embedding, "
            "using the currently-configured embedding_provider."
        )
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Rows per batch embedding call (default {DEFAULT_BATCH_SIZE}).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    exit_code = asyncio.run(_main_async(args.batch_size))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
