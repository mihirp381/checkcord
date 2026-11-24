"""Tests for provenance receipts.

Covers ``content_hash``/``last_verified_at`` on ``CanonicalContext``
(populated at creation and kept in sync with
``recalculate_confidence_scores``) and the receipt shape returned by
``get_top_contexts_for_repo``, ``get_org_global_top_contexts`` and
``find_similar_contexts``: ``content_hash``, ``last_verified_at`` and a
summarized ``sources`` citation list.
"""

import asyncio
import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text

from checkcord.core.config import get_settings
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.storage.postgres import (
    PostgresEventStore,
    create_checkcord_engine,
    tenant_context,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _make_event(tenant_id: object, source_id: str) -> CanonicalEvent:
    return CanonicalEvent(
        tenant_id=tenant_id,
        source_platform=SourcePlatform.SLACK,
        source_type=SourceType.MESSAGE,
        source_id=source_id,
        happened_at=datetime.now(UTC),
        author={"source_id": "user_test"},
        content_raw="Some raw content nobody should see in the receipt list",
        content_clean="Some raw content nobody should see in the receipt list",
        access_control={"is_public": True},
    )


async def test_save_canonical_context_populates_content_hash_and_last_verified_at() -> None:
    """Both fields are non-null immediately at creation."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="A freshly synthesized insight",
                confidence_score=1.0,
            )
            ctx = await store.get_canonical_context(tenant_id, ctx_id)
            assert ctx is not None
            assert ctx["content_hash"]
            assert (
                ctx["content_hash"] == hashlib.sha256(b"A freshly synthesized insight").hexdigest()
            )
            assert ctx["last_verified_at"] is not None
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_save_canonical_context_recomputes_hash_when_summary_changes() -> None:
    """content_hash must never go stale relative to the summary currently stored.

    save_canonical_context is the sole write path for `summary` -- if it is
    ever called again for the same id with different text (e.g. an idempotent
    retry with corrected content), content_hash must be recomputed to match,
    not left pointing at the original text.
    """
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    ctx_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Original text",
                confidence_score=1.0,
                id=ctx_id,
            )
            first = await store.get_canonical_context(tenant_id, ctx_id)
            assert first is not None
            first_hash = first["content_hash"]
            first_verified_at = first["last_verified_at"]

            await asyncio.sleep(0.05)

            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Corrected text",
                confidence_score=1.0,
                id=ctx_id,
            )
            second = await store.get_canonical_context(tenant_id, ctx_id)
            assert second is not None

            assert second["content_hash"] == hashlib.sha256(b"Corrected text").hexdigest()
            assert second["content_hash"] != first_hash
            assert second["last_verified_at"] > first_verified_at
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_update_context_scores_batch_global_updates_last_verified_at_selectively() -> None:
    """last_verified_at means "last actually re-evaluated."

    A context included in a confidence-score batch update gets last_verified_at
    bumped forward. A context not included keeps its prior value.
    """
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    changed_id = uuid4()
    unchanged_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Will be recomputed",
                confidence_score=1.0,
                id=changed_id,
            )
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="No new inputs, untouched",
                confidence_score=1.0,
                id=unchanged_id,
            )
            before_changed = await store.get_canonical_context(tenant_id, changed_id)
            before_unchanged = await store.get_canonical_context(tenant_id, unchanged_id)
            assert before_changed is not None
            assert before_unchanged is not None

            await asyncio.sleep(0.05)

            updated_count = await store.update_context_scores_batch_global(
                [(tenant_id, changed_id, 5.0)]
            )
            assert updated_count == 1

            after_changed = await store.get_canonical_context(tenant_id, changed_id)
            after_unchanged = await store.get_canonical_context(tenant_id, unchanged_id)
            assert after_changed is not None
            assert after_unchanged is not None

            assert after_changed["confidence_score"] == 5.0
            assert after_changed["last_verified_at"] > before_changed["last_verified_at"]

            assert after_unchanged["confidence_score"] == 1.0
            assert after_unchanged["last_verified_at"] == before_unchanged["last_verified_at"]
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_get_top_contexts_for_repo_includes_receipt_fields() -> None:
    """Decisions carry content_hash/last_verified_at/sources."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    repo_id = "acme/service-a"
    ctx_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Use asyncio everywhere",
                confidence_score=9.0,
                id=ctx_id,
                scope_repo_identifier=repo_id,
            )
            event = _make_event(tenant_id, f"msg_{uuid4().hex[:8]}")
            await store.save_event(event)
            await store.link_context_to_event(tenant_id, ctx_id, event.event_id)

            results = await store.get_top_contexts_for_repo(tenant_id, repo_id)
            assert len(results) == 1
            r = results[0]

            assert r["id"] == str(ctx_id)
            assert r["content_hash"] == hashlib.sha256(b"Use asyncio everywhere").hexdigest()
            assert r["last_verified_at"] is not None
            assert isinstance(r["last_verified_at"], str)  # isoformat, JSON-serializable

            assert len(r["sources"]) == 1
            source = r["sources"][0]
            assert source["event_id"] == str(event.event_id)
            assert source["source_platform"] == SourcePlatform.SLACK.value
            assert source["source_type"] == SourceType.MESSAGE.value
            assert source["happened_at"] is not None
            # Full quote text is deliberately omitted from this list-returning tool.
            assert "content_clean" not in source
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_event_links"))
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM events WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_get_top_contexts_for_repo_zero_linked_events_returns_empty_sources_list() -> None:
    """Edge case: a context with no linked events returns sources: [], never a missing key."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    repo_id = "acme/service-a"

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="No sources yet",
                confidence_score=9.0,
                scope_repo_identifier=repo_id,
            )

            results = await store.get_top_contexts_for_repo(tenant_id, repo_id)
            assert len(results) == 1
            assert results[0]["sources"] == []
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_get_org_global_top_contexts_receipt_shape_and_scope() -> None:
    """get_org_global_top_contexts: receipts + scope filter."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Org-global insight",
                confidence_score=9.0,
            )
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Repo-local insight",
                confidence_score=9.5,
                scope_repo_identifier="acme/service-a",
            )

            results = await store.get_org_global_top_contexts(tenant_id)
            summaries = {r["summary"] for r in results}
            assert summaries == {"Org-global insight"}

            r = results[0]
            assert r["content_hash"] == hashlib.sha256(b"Org-global insight").hexdigest()
            assert r["last_verified_at"] is not None
            assert r["sources"] == []
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_find_similar_contexts_includes_receipts_by_default() -> None:
    """search_conceptual_context's default (include_sources=True) is receipts-enriched."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    embedding = [0.1] * 1024
    ctx_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Vector-searchable insight",
                confidence_score=5.0,
                embedding=embedding,
                id=ctx_id,
            )
            event = _make_event(tenant_id, f"msg_{uuid4().hex[:8]}")
            await store.save_event(event)
            await store.link_context_to_event(tenant_id, ctx_id, event.event_id)

            results = await store.find_similar_contexts(tenant_id, embedding, threshold=0.5)
            assert len(results) == 1
            r = results[0]

            assert r["id"] == str(ctx_id)
            assert r["content_hash"] == hashlib.sha256(b"Vector-searchable insight").hexdigest()
            assert isinstance(r["last_verified_at"], str)
            assert len(r["sources"]) == 1
            assert r["sources"][0]["event_id"] == str(event.event_id)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_event_links"))
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM events WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_find_similar_contexts_include_sources_false_skips_sources_entirely() -> None:
    """ClusteringWorker's dedup path opts out of the sources join (perf on the ingestion hot path).

    include_sources=False must omit the `sources` key entirely (not just
    return an empty list) -- this is what lets the caller skip the extra
    batch query, not merely skip formatting its result.
    """
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    embedding = [0.1] * 1024
    ctx_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Dedup candidate",
                confidence_score=5.0,
                embedding=embedding,
                id=ctx_id,
            )
            event = _make_event(tenant_id, f"msg_{uuid4().hex[:8]}")
            await store.save_event(event)
            await store.link_context_to_event(tenant_id, ctx_id, event.event_id)

            results = await store.find_similar_contexts(
                tenant_id, embedding, threshold=0.5, include_sources=False
            )
            assert len(results) == 1
            assert "sources" not in results[0]
            # content_hash/last_verified_at are free (same row, no extra query)
            # and stay populated regardless of include_sources.
            assert results[0]["content_hash"] == hashlib.sha256(b"Dedup candidate").hexdigest()
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_event_links"))
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM events WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()
