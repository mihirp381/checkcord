"""Tests for the per-user RESTRICTIVE RLS policies.

A RESTRICTIVE policy on ``canonical_contexts`` and ``events``, on top of the
tenant-isolation policy, that does not apply when ``app.current_user_id`` is
unset.
"""

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


async def test_canonical_contexts_user_rls_visible_invisible_public_and_override() -> None:
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    await _setup_rls_test_user(engine)

    tenant_id = uuid4()
    user_a, user_b, admin_c = uuid4(), uuid4(), uuid4()

    try:
        # Seed data as superuser (RLS bypassed) via the store, but under a
        # tenant_context so tenant scoping is respected.
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)

            private_ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="private to user_a and admin override for admin_c",
                confidence_score=0.0,
                access_control={
                    "is_public": False,
                    "visibility": "restricted",
                    "acl_type": "open",
                    "allowed_identity_ids": [str(user_a)],
                    "source_group_id": None,
                    "inherited": False,
                    "admin_overrides": [str(admin_c)],
                },
            )
            public_ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="public to everyone",
                confidence_score=0.0,
                access_control={
                    "is_public": True,
                    "visibility": "public",
                    "acl_type": "open",
                    "allowed_identity_ids": [],
                    "source_group_id": None,
                    "inherited": False,
                    "admin_overrides": [],
                },
            )

        # --- No user_id set: backward-compatible, sees everything (as today) ---
        async with tenant_context(engine, tenant_id) as conn:
            await conn.execute(text("SET LOCAL ROLE rls_test_user"))
            rows = (await conn.execute(text("SELECT id FROM canonical_contexts"))).fetchall()
            assert {r[0] for r in rows} == {private_ctx_id, public_ctx_id}

        # --- user_a: in allowed_identity_ids -> sees the private ctx + public ctx ---
        async with tenant_context(engine, tenant_id, user_id=user_a) as conn:
            await conn.execute(text("SET LOCAL ROLE rls_test_user"))
            rows = (await conn.execute(text("SELECT id FROM canonical_contexts"))).fetchall()
            assert {r[0] for r in rows} == {private_ctx_id, public_ctx_id}

        # --- user_b: not in allowed_identity_ids, not admin -> sees only public ---
        async with tenant_context(engine, tenant_id, user_id=user_b) as conn:
            await conn.execute(text("SET LOCAL ROLE rls_test_user"))
            rows = (await conn.execute(text("SELECT id FROM canonical_contexts"))).fetchall()
            assert {r[0] for r in rows} == {public_ctx_id}

        # --- admin_c: in admin_overrides only -> still sees the private ctx ---
        async with tenant_context(engine, tenant_id, user_id=admin_c) as conn:
            await conn.execute(text("SET LOCAL ROLE rls_test_user"))
            rows = (await conn.execute(text("SELECT id FROM canonical_contexts"))).fetchall()
            assert {r[0] for r in rows} == {private_ctx_id, public_ctx_id}
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
        await engine.dispose()


async def test_events_user_rls_visible_invisible_public_and_override() -> None:
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    await _setup_rls_test_user(engine)

    tenant_id = uuid4()
    user_a, user_b, admin_c = uuid4(), uuid4(), uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)

            private_event = CanonicalEvent(
                tenant_id=tenant_id,
                source_platform=SourcePlatform.SLACK,
                source_type=SourceType.MESSAGE,
                source_id="msg_private",
                happened_at=datetime.now(UTC),
                author={"source_id": "user_test"},
                content_raw="private content",
                content_clean="private content",
                access_control={
                    "is_public": False,
                    "allowed_identity_ids": [str(user_a)],
                    "admin_overrides": [str(admin_c)],
                },
            )
            await store.save_event(private_event)

            public_event = CanonicalEvent(
                tenant_id=tenant_id,
                source_platform=SourcePlatform.SLACK,
                source_type=SourceType.MESSAGE,
                source_id="msg_public",
                happened_at=datetime.now(UTC),
                author={"source_id": "user_test"},
                content_raw="public content",
                content_clean="public content",
                access_control={"is_public": True},
            )
            await store.save_event(public_event)

        async def _visible_event_ids(user_id: object | None) -> set:
            async with tenant_context(engine, tenant_id, user_id=user_id) as conn:
                await conn.execute(text("SET LOCAL ROLE rls_test_user"))
                rows = (
                    await conn.execute(
                        text("SELECT event_id FROM events WHERE tenant_id = :tid"),
                        {"tid": tenant_id},
                    )
                ).fetchall()
                return {r[0] for r in rows}

        assert await _visible_event_ids(None) == {
            private_event.event_id,
            public_event.event_id,
        }
        assert await _visible_event_ids(user_a) == {
            private_event.event_id,
            public_event.event_id,
        }
        assert await _visible_event_ids(user_b) == {public_event.event_id}
        assert await _visible_event_ids(admin_c) == {
            private_event.event_id,
            public_event.event_id,
        }
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM events WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_tenant_context_without_user_id_is_fully_backward_compatible() -> None:
    """A connection that never sets app.current_user_id is unaffected by the
    per-user policies.
    """
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    await _setup_rls_test_user(engine)

    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            # Fully private, empty allowed_identity_ids -- would be invisible
            # to any *set* user_id, but must remain visible when user_id is
            # never set at all (the pipeline/worker code path).
            ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="fail-closed defaults, but user_id was never set",
                confidence_score=0.0,
            )

        async with tenant_context(engine, tenant_id) as conn:
            await conn.execute(text("SET LOCAL ROLE rls_test_user"))
            row = (
                await conn.execute(
                    text("SELECT id FROM canonical_contexts WHERE id = :cid"), {"cid": ctx_id}
                )
            ).fetchone()
            assert row is not None
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()
