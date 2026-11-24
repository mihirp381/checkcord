"""Tests for per-user access enforcement on the retrieval paths.

``test_user_rls.py`` proves the RESTRICTIVE RLS policies at the raw-SQL
level. These tests call ``get_top_contexts_for_repo``,
``get_org_global_top_contexts`` and ``find_similar_contexts`` under a
non-superuser role with ``tenant_context(..., user_id=...)``. The first test
combines the scope filter with the access filter in one scenario.
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


async def _setup_rls_test_user(engine: object) -> None:
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


def _access(is_public: bool, allowed: list[str] | None = None) -> dict:
    return {
        "is_public": is_public,
        "visibility": "public" if is_public else "restricted",
        "acl_type": "open",
        "allowed_identity_ids": allowed or [],
        "source_group_id": None,
        "inherited": False,
        "admin_overrides": [],
    }


async def test_get_top_contexts_for_repo_combines_scope_and_access_filters() -> None:
    """Scope filter and access filter applied together."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    await _setup_rls_test_user(engine)

    tenant_id = uuid4()
    user_a, user_b = uuid4(), uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Org-global rule, public",
                confidence_score=5.0,
                scope_repo_identifier=None,
                access_control=_access(is_public=True),
            )
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Repo-local rule, private to user_a",
                confidence_score=4.0,
                scope_repo_identifier="acme/service-a",
                access_control=_access(is_public=False, allowed=[str(user_a)]),
            )
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Repo-local rule, private to user_b",
                confidence_score=3.0,
                scope_repo_identifier="acme/service-a",
                access_control=_access(is_public=False, allowed=[str(user_b)]),
            )

        # user_a: sees the public org-global rule + their own repo-local rule,
        # never user_b's -- scope says both repo-local rows are in-scope,
        # access must still narrow it down to one.
        async with tenant_context(engine, tenant_id, user_id=user_a) as conn:
            await conn.execute(text("SET LOCAL ROLE rls_test_user"))
            store = PostgresEventStore(conn)
            results = await store.get_top_contexts_for_repo(tenant_id, "acme/service-a")
            summaries = {r["summary"] for r in results}
            assert summaries == {
                "Org-global rule, public",
                "Repo-local rule, private to user_a",
            }

        async with tenant_context(engine, tenant_id, user_id=user_b) as conn:
            await conn.execute(text("SET LOCAL ROLE rls_test_user"))
            store = PostgresEventStore(conn)
            results = await store.get_top_contexts_for_repo(tenant_id, "acme/service-a")
            summaries = {r["summary"] for r in results}
            assert summaries == {
                "Org-global rule, public",
                "Repo-local rule, private to user_b",
            }
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_get_org_global_top_contexts_respects_per_user_access() -> None:
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    await _setup_rls_test_user(engine)

    tenant_id = uuid4()
    user_a = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Public org decision",
                confidence_score=5.0,
                access_control=_access(is_public=True),
            )
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Private org decision for user_a",
                confidence_score=4.0,
                access_control=_access(is_public=False, allowed=[str(user_a)]),
            )

        async with tenant_context(engine, tenant_id, user_id=uuid4()) as conn:
            await conn.execute(text("SET LOCAL ROLE rls_test_user"))
            store = PostgresEventStore(conn)
            results = await store.get_org_global_top_contexts(tenant_id)
            summaries = {r["summary"] for r in results}
            assert summaries == {"Public org decision"}

        async with tenant_context(engine, tenant_id, user_id=user_a) as conn:
            await conn.execute(text("SET LOCAL ROLE rls_test_user"))
            store = PostgresEventStore(conn)
            results = await store.get_org_global_top_contexts(tenant_id)
            summaries = {r["summary"] for r in results}
            assert summaries == {"Public org decision", "Private org decision for user_a"}
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_find_similar_contexts_respects_per_user_access() -> None:
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    await _setup_rls_test_user(engine)

    tenant_id = uuid4()
    user_a, user_b = uuid4(), uuid4()
    embedding = [0.1] * 1024

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Visible to user_a only",
                confidence_score=1.0,
                embedding=embedding,
                access_control=_access(is_public=False, allowed=[str(user_a)]),
            )

        async with tenant_context(engine, tenant_id, user_id=user_a) as conn:
            await conn.execute(text("SET LOCAL ROLE rls_test_user"))
            store = PostgresEventStore(conn)
            results = await store.find_similar_contexts(tenant_id, embedding, threshold=0.5)
            assert {r["summary"] for r in results} == {"Visible to user_a only"}

        async with tenant_context(engine, tenant_id, user_id=user_b) as conn:
            await conn.execute(text("SET LOCAL ROLE rls_test_user"))
            store = PostgresEventStore(conn)
            results = await store.find_similar_contexts(tenant_id, embedding, threshold=0.5)
            assert results == []
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_anonymous_sentinel_user_sees_public_only() -> None:
    """ANONYMOUS_USER_ID is a concrete user id, so it sees public content only,
    never the unfiltered view a null user id gives internal workers.
    """
    from checkcord.core.identity import ANONYMOUS_USER_ID

    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    await _setup_rls_test_user(engine)

    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Public decision",
                confidence_score=5.0,
                access_control=_access(is_public=True),
            )
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Private decision, nobody in particular",
                confidence_score=4.0,
                access_control=_access(is_public=False, allowed=[str(uuid4())]),
            )

        async with tenant_context(engine, tenant_id, user_id=ANONYMOUS_USER_ID) as conn:
            await conn.execute(text("SET LOCAL ROLE rls_test_user"))
            store = PostgresEventStore(conn)
            results = await store.get_org_global_top_contexts(tenant_id)
            assert {r["summary"] for r in results} == {"Public decision"}
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()
