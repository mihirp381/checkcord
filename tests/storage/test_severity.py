"""Tests for CanonicalContext.severity and its storage methods.

Real Postgres.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text

from checkcord.core.config import get_settings
from checkcord.models.enums import Severity, SourcePlatform, SourceType
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
        content_raw="Let's ship the new severity flow",
        content_clean="Let's ship the new severity flow",
        access_control={"is_public": True},
    )


async def test_save_canonical_context_defaults_severity_to_soft_warn() -> None:
    """New contexts start soft_warn."""
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
            assert ctx["severity"] == Severity.SOFT_WARN.value
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()
