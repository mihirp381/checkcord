from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.core.config import get_settings
from checkcord.core.startup import database_role_flags, database_role_problem
from checkcord.models.event import CanonicalEvent, SourcePlatform, SourceType
from checkcord.storage.models import CanonicalContext
from checkcord.storage.postgres import PostgresEventStore, create_checkcord_engine, tenant_context

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ROLES_SQL = Path(__file__).resolve().parents[2] / "deploy" / "postgres" / "roles.sql"
PASSWORD = "app-role-test"


def _event(tenant_id: UUID, content: str) -> CanonicalEvent:
    return CanonicalEvent(
        tenant_id=tenant_id,
        source_platform=SourcePlatform.SLACK,
        source_type=SourceType.MESSAGE,
        source_id=f"msg_{uuid4().hex[:8]}",
        happened_at=datetime.now(UTC),
        author={"source_id": "user_test"},
        content_raw=content,
        content_clean=content,
        access_control={"is_public": True},
    )


@pytest_asyncio.fixture
async def app_engine() -> AsyncGenerator[AsyncEngine, None]:
    """An engine logged in as a throwaway member of checkcord_app, the runtime role."""
    admin_url = make_url(get_settings().database_url)
    role = f"checkcord_app_test_{uuid4().hex[:8]}"
    admin = await asyncpg.connect(admin_url.set(drivername="postgresql").render_as_string(False))
    try:
        await admin.execute(ROLES_SQL.read_text())
        await admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{PASSWORD}' IN ROLE checkcord_app")
        engine = create_checkcord_engine(
            admin_url.set(username=role, password=PASSWORD).render_as_string(False)
        )
        try:
            yield engine
        finally:
            await engine.dispose()
    finally:
        await admin.execute(f"DROP ROLE IF EXISTS {role}")
        await admin.close()


async def test_app_role_is_not_exempt_from_rls(app_engine: AsyncEngine) -> None:
    role, superuser, bypassrls = await database_role_flags(app_engine)
    assert role.startswith("checkcord_app_test_")
    assert (superuser, bypassrls) == (False, False)
    assert database_role_problem(role, superuser, bypassrls) is None


async def test_app_role_isolates_tenants(app_engine: AsyncEngine) -> None:
    tenant_a, tenant_b = uuid4(), uuid4()
    event_a, event_b = _event(tenant_a, "alpha"), _event(tenant_b, "beta")

    async with tenant_context(app_engine, tenant_a) as conn:
        store = PostgresEventStore(conn)
        assert await store.save_event(event_a)
        context_a = await store.save_canonical_context(tenant_a, "alpha context", 0.5)
    async with tenant_context(app_engine, tenant_b) as conn:
        store = PostgresEventStore(conn)
        assert await store.save_event(event_b)
        context_b = await store.save_canonical_context(tenant_b, "beta context", 0.5)

    try:
        async with tenant_context(app_engine, tenant_a) as conn:
            store = PostgresEventStore(conn)
            assert await store.get_events_by_ids(tenant_a, [event_a.event_id])
            assert not await store.get_events_by_ids(tenant_b, [event_b.event_id])
            assert (await store.get_canonical_context(tenant_a, context_a)) is not None
            assert (await store.get_canonical_context(tenant_b, context_b)) is None
            visible = await conn.scalar(
                text("SELECT count(*) FROM events WHERE event_id = ANY(:ids)"),
                {"ids": [event_a.event_id, event_b.event_id]},
            )
            assert visible == 1
            contexts = await conn.scalar(
                select(func.count())
                .select_from(CanonicalContext)
                .where(CanonicalContext.id.in_([context_a, context_b]))
            )
            assert contexts == 1

        async with app_engine.connect() as conn:
            unscoped = await conn.scalar(
                text("SELECT count(*) FROM events WHERE event_id = ANY(:ids)"),
                {"ids": [event_a.event_id, event_b.event_id]},
            )
            assert unscoped == 0

        with pytest.raises(DBAPIError, match="row-level security"):
            async with tenant_context(app_engine, tenant_a) as conn:
                await PostgresEventStore(conn).save_event(_event(tenant_b, "smuggled"))
    finally:
        for tenant, event, context in (
            (tenant_a, event_a, context_a),
            (tenant_b, event_b, context_b),
        ):
            async with tenant_context(app_engine, tenant) as conn:
                await conn.execute(
                    text("DELETE FROM events WHERE event_id = :id"), {"id": event.event_id}
                )
                await conn.execute(
                    text("DELETE FROM canonical_contexts WHERE id = :id"), {"id": context}
                )
