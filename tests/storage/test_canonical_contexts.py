from uuid import uuid4

import numpy as np
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.core.config import get_settings
from checkcord.storage.postgres import PostgresEventStore, tenant_context

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_canonical_contexts_crud_and_rls() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    # 0. Setup non-superuser role for RLS testing (idempotent)
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
        # 1. Test basic CRUD under tenant context as rls_test_user
        async with tenant_context(engine, tenant_a) as conn_a:
            await conn_a.execute(text("SET LOCAL ROLE rls_test_user"))
            store_a = PostgresEventStore(conn_a)

            # Save context
            context_id_a = await store_a.save_canonical_context(
                tenant_id=tenant_a,
                summary="This is context for tenant A",
                confidence_score=0.9,
                embedding=[0.1] * 1024,
            )

            # Get context
            ctx = await store_a.get_canonical_context(tenant_a, context_id_a)
            assert ctx is not None
            assert ctx["summary"] == "This is context for tenant A"
            assert ctx["confidence_score"] == 0.9
            assert ctx["embedding"] is not None
            assert np.allclose(ctx["embedding"], [0.1] * 1024)

            # Link to events
            event_id_a1 = uuid4()
            event_id_a2 = uuid4()
            assert await store_a.link_context_to_event(tenant_a, context_id_a, event_id_a1) is True
            assert await store_a.link_context_to_event(tenant_a, context_id_a, event_id_a2) is True

            # Check links
            events = await store_a.get_events_for_context(tenant_a, context_id_a)
            assert set(events) == {event_id_a1, event_id_a2}

        # 2. Test strict RLS isolation
        async with tenant_context(engine, tenant_b) as conn_b:
            await conn_b.execute(text("SET LOCAL ROLE rls_test_user"))
            store_b = PostgresEventStore(conn_b)

            # Try to read Tenant A's context under Tenant B context
            ctx_b = await store_b.get_canonical_context(tenant_a, context_id_a)
            assert ctx_b is None  # Blocked by RLS

            # Try to read Tenant A's links under Tenant B context
            events_b = await store_b.get_events_for_context(tenant_a, context_id_a)
            assert events_b == []  # Blocked by RLS

            # Create Tenant B context
            context_id_b = await store_b.save_canonical_context(
                tenant_id=tenant_b,
                summary="This is context for tenant B",
                confidence_score=0.5,
                embedding=[0.2] * 1024,
            )

            ctx_b_real = await store_b.get_canonical_context(tenant_b, context_id_b)
            assert ctx_b_real is not None
            assert ctx_b_real["summary"] == "This is context for tenant B"

    finally:
        # Clean up (using standard superuser engine, RLS bypassed)
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_event_links"))
            await conn.execute(text("DELETE FROM canonical_contexts"))
        await engine.dispose()
