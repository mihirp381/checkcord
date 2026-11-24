from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.core.config import get_settings
from checkcord.storage.postgres import PostgresEventStore, tenant_context

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_postgres_vector_similarity_search() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    # Setup roles for RLS (idempotent)
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

    tenant_a = uuid4()
    tenant_b = uuid4()

    try:
        # Create context vectors
        # A base vector [0.1] * 1024
        vec_base = [0.1] * 1024
        # A very similar vector (almost identical)
        vec_similar = [0.1] * 1024
        vec_similar[0] = 0.1001
        # A dissimilar vector
        vec_dissimilar = [-0.1] * 1024

        async with tenant_context(engine, tenant_a) as conn_a:
            await conn_a.execute(text("SET LOCAL ROLE rls_test_user"))
            store_a = PostgresEventStore(conn_a)

            # Insert context with base vector
            await store_a.save_canonical_context(
                tenant_id=tenant_a,
                summary="Base context for Tenant A",
                confidence_score=0.9,
                embedding=vec_base,
            )

            # Search with a similar vector -> should match (>0.85 similarity)
            matches = await store_a.find_similar_contexts(
                tenant_id=tenant_a,
                embedding=vec_similar,
                threshold=0.85,
            )
            assert len(matches) == 1
            assert matches[0]["summary"] == "Base context for Tenant A"
            assert matches[0]["similarity"] > 0.99

            # Search with a dissimilar vector -> should not match (>0.85 similarity)
            matches_empty = await store_a.find_similar_contexts(
                tenant_id=tenant_a,
                embedding=vec_dissimilar,
                threshold=0.85,
            )
            assert len(matches_empty) == 0

        # Verify tenant isolation: B searching with vec_similar should find nothing
        async with tenant_context(engine, tenant_b) as conn_b:
            await conn_b.execute(text("SET LOCAL ROLE rls_test_user"))
            store_b = PostgresEventStore(conn_b)

            # Search B (empty database for B)
            matches_b = await store_b.find_similar_contexts(
                tenant_id=tenant_b,
                embedding=vec_similar,
                threshold=0.85,
            )
            assert len(matches_b) == 0

            # Even if B uses tenant_a argument, RLS blocks it
            matches_b_leak = await store_b.find_similar_contexts(
                tenant_id=tenant_a,
                embedding=vec_similar,
                threshold=0.85,
            )
            assert len(matches_b_leak) == 0

    finally:
        # Clean up
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM canonical_contexts"))
        await engine.dispose()


# ---------------------------------------------------------------------------
# Hybrid lexical + vector search via Reciprocal Rank Fusion.
# ---------------------------------------------------------------------------


async def test_hybrid_search_recovers_exact_match_below_vector_threshold() -> None:
    """A context whose embedding similarity alone falls below the threshold
    still surfaces when its summary contains an exact string the query text
    also contains.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    tenant_id = uuid4()

    # Opposite vectors -> cosine similarity -1.0, far below any real threshold.
    query_embedding = [0.1] * 1024
    unrelated_but_close_embedding = [0.1] * 1024
    unrelated_but_close_embedding[0] = 0.1001
    exact_match_but_far_embedding = [-0.1] * 1024

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)

            # Context A: contains the exact identifier the query will search
            # for, but its embedding is the opposite of the query embedding
            # -- pure vector search at threshold=0.3 would never surface it.
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Investigate error code XKCD-7742 in the billing pipeline",
                confidence_score=0.5,
                embedding=exact_match_but_far_embedding,
            )
            # Context B: high vector similarity to the query, but its summary
            # has no lexical overlap with the query text at all.
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Unrelated decision about caching strategy",
                confidence_score=0.5,
                embedding=unrelated_but_close_embedding,
            )

            # Pure-vector baseline (no query_text): confirms the below-threshold
            # case really is invisible to vector-only search, as claimed.
            vector_only = await store.find_similar_contexts(
                tenant_id=tenant_id,
                embedding=query_embedding,
                threshold=0.3,
            )
            assert all("XKCD-7742" not in r["summary"] for r in vector_only), (
                "exact-match context should not be reachable via vector-only search"
            )

            # Hybrid (query_text set): the lexical candidate list recovers it.
            hybrid = await store.find_similar_contexts(
                tenant_id=tenant_id,
                embedding=query_embedding,
                threshold=0.3,
                query_text="XKCD-7742",
            )
            summaries = [r["summary"] for r in hybrid]
            assert any("XKCD-7742" in s for s in summaries), (
                "hybrid search should recover the exact-match context via RRF fusion"
            )
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM canonical_contexts"))
        await engine.dispose()


async def test_hybrid_search_with_no_lexical_match_falls_back_to_vector_only() -> None:
    """Edge case: a query_text with no lexical match anywhere must not error
    or suppress the vector-only results -- RRF degrades gracefully when a
    candidate (or here, an entire ranked list) is empty."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    tenant_id = uuid4()

    query_embedding = [0.1] * 1024
    close_embedding = [0.1] * 1024
    close_embedding[0] = 0.1001

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="A decision with no relation to the search phrase",
                confidence_score=0.5,
                embedding=close_embedding,
            )

            results = await store.find_similar_contexts(
                tenant_id=tenant_id,
                embedding=query_embedding,
                threshold=0.85,
                query_text="zzz_no_lexical_overlap_whatsoever_zzz",
            )
            assert len(results) == 1
            assert results[0]["summary"] == "A decision with no relation to the search phrase"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM canonical_contexts"))
        await engine.dispose()


# ---------------------------------------------------------------------------
# Optional `limit` param: caps the vector query itself rather than truncating
# an unbounded fetch.
# ---------------------------------------------------------------------------


async def test_find_similar_contexts_limit_caps_vector_query() -> None:
    """`limit=N` returns at most N rows even when more clear the threshold;
    omitting it leaves the query unbounded.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    tenant_id = uuid4()

    query_embedding = [0.1] * 1024

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            for i in range(5):
                close_embedding = [0.1] * 1024
                close_embedding[0] = 0.1 + (i * 0.00001)
                await store.save_canonical_context(
                    tenant_id=tenant_id,
                    summary=f"Context {i}",
                    confidence_score=0.5,
                    embedding=close_embedding,
                )

            unbounded = await store.find_similar_contexts(
                tenant_id=tenant_id,
                embedding=query_embedding,
                threshold=0.85,
            )
            assert len(unbounded) == 5, "omitting limit must preserve exact prior behavior"

            capped = await store.find_similar_contexts(
                tenant_id=tenant_id,
                embedding=query_embedding,
                threshold=0.85,
                limit=2,
            )
            assert len(capped) == 2
            # The cap keeps the two closest matches (SQL LIMIT after
            # ORDER BY distance), not an arbitrary two.
            assert capped[0]["similarity"] >= capped[1]["similarity"]
            top_two_by_similarity = sorted(unbounded, key=lambda r: r["similarity"], reverse=True)[
                :2
            ]
            assert {r["summary"] for r in capped} == {r["summary"] for r in top_two_by_similarity}
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM canonical_contexts"))
        await engine.dispose()
