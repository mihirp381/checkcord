"""Tests for the personalization store methods: user_repo_affinity read/write
and the org-global-id lookup apply_affinity_boost needs.
"""

from uuid import uuid4

import pytest
from sqlalchemy import text

from checkcord.core.config import get_settings
from checkcord.storage.postgres import (
    PostgresEventStore,
    create_checkcord_engine,
    tenant_context,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_get_user_repo_affinity_returns_zero_when_never_queried() -> None:
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    user_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id, user_id=user_id) as conn:
            store = PostgresEventStore(conn)
            count = await store.get_user_repo_affinity(tenant_id, user_id, "acme/service-a")
            assert count == 0
    finally:
        await engine.dispose()


async def test_record_user_repo_query_increments_count() -> None:
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    user_id = uuid4()
    repo = "acme/service-a"

    try:
        async with tenant_context(engine, tenant_id, user_id=user_id) as conn:
            store = PostgresEventStore(conn)
            await store.record_user_repo_query(tenant_id, user_id, repo)
            assert await store.get_user_repo_affinity(tenant_id, user_id, repo) == 1

            await store.record_user_repo_query(tenant_id, user_id, repo)
            await store.record_user_repo_query(tenant_id, user_id, repo)
            assert await store.get_user_repo_affinity(tenant_id, user_id, repo) == 3
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM user_repo_affinity WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_record_user_repo_query_is_per_user_and_per_repo() -> None:
    """Two different users, or the same user querying two different repos,
    must not share an affinity counter."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    user_a = uuid4()
    user_b = uuid4()

    try:
        async with tenant_context(engine, tenant_id, user_id=user_a) as conn:
            store = PostgresEventStore(conn)
            await store.record_user_repo_query(tenant_id, user_a, "acme/service-a")
            await store.record_user_repo_query(tenant_id, user_a, "acme/service-a")
            await store.record_user_repo_query(tenant_id, user_a, "acme/service-b")

        async with tenant_context(engine, tenant_id, user_id=user_b) as conn:
            store = PostgresEventStore(conn)
            await store.record_user_repo_query(tenant_id, user_b, "acme/service-a")

        async with tenant_context(engine, tenant_id, user_id=user_a) as conn:
            store = PostgresEventStore(conn)
            assert await store.get_user_repo_affinity(tenant_id, user_a, "acme/service-a") == 2
            assert await store.get_user_repo_affinity(tenant_id, user_a, "acme/service-b") == 1
        async with tenant_context(engine, tenant_id, user_id=user_b) as conn:
            store = PostgresEventStore(conn)
            assert await store.get_user_repo_affinity(tenant_id, user_b, "acme/service-a") == 1
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM user_repo_affinity WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_get_org_global_ids_distinguishes_scoped_from_global() -> None:
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            org_ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id, summary="Org-global", confidence_score=1.0
            )
            scoped_ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Repo-scoped",
                confidence_score=1.0,
                scope_repo_identifier="acme/service-a",
            )

            org_global_ids = await store.get_org_global_ids(tenant_id, [org_ctx_id, scoped_ctx_id])
            assert org_global_ids == {org_ctx_id}
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_repo_scopes"))
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_get_org_global_ids_accepts_string_ids_from_real_caller() -> None:
    """find_similar_contexts (search_conceptual_context's real
    caller) returns already-stringified ids, not UUID objects -- a plain
    set-difference between strings and UUID rows would silently classify
    every scoped context as org-global (str(x) != UUID(x)). This is the
    actual production calling convention, not the UUID-object convention
    the other test in this file exercises."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            org_ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id, summary="Org-global", confidence_score=1.0
            )
            scoped_ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Repo-scoped",
                confidence_score=1.0,
                scope_repo_identifier="acme/service-a",
            )

            org_global_ids = await store.get_org_global_ids(
                tenant_id, [str(org_ctx_id), str(scoped_ctx_id)]
            )
            assert org_global_ids == {org_ctx_id}
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_repo_scopes"))
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_get_org_global_ids_empty_input_returns_empty_set() -> None:
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            assert await store.get_org_global_ids(tenant_id, []) == set()
    finally:
        await engine.dispose()
