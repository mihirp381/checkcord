"""Integration tests for MeetingExtractionStore and RLS policies on meeting extraction tables."""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.core.config import get_settings
from checkcord.models.enums import ConstraintType, RiskLevel
from checkcord.models.extractions import (
    DiscourseSegment,
    ExtractedADR,
    ExtractedConstraint,
    ExtractedRisk,
)
from checkcord.storage.meeting_extractions import MeetingExtractionStore
from checkcord.storage.postgres import tenant_context

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_meeting_extraction_store_and_rls_isolation() -> None:
    """Verify insertion and strict cross-tenant RLS isolation for meeting extractions."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    store = MeetingExtractionStore()

    # Setup non-superuser role for RLS testing
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
    parent_event_id = uuid4()

    segment_a = DiscourseSegment(
        parent_event_id=parent_event_id,
        tenant_id=tenant_a,
        text_content="Alice: We decided to adopt Postgres RLS.",
        topics=["database"],
        speakers=["Alice"],
    )

    adr_a = ExtractedADR(
        decision_title="Adopt Postgres RLS",
        context="Multi-tenant isolation requirement",
        final_decision="Postgres RLS",
        primary_driver="Alice",
    )

    constraint_a = ExtractedConstraint(
        constraint_type=ConstraintType.REGULATORY,
        description="GDPR compliance",
        source_quote="Alice: We decided to adopt Postgres RLS.",
    )

    risk_a = ExtractedRisk(
        risk_level=RiskLevel.HIGH,
        description="Migration downtime risk",
    )

    # 1. Insert as Tenant A
    async with tenant_context(engine, tenant_a) as conn:
        await conn.execute(text("SET LOCAL ROLE rls_test_user"))
        await store.save_segment_with_extractions(
            conn, segment_a, [adr_a], [constraint_a], [risk_a]
        )

    # 2. Query as Tenant A -> Should see all inserted records
    async with tenant_context(engine, tenant_a) as conn:
        await conn.execute(text("SET LOCAL ROLE rls_test_user"))
        res_seg = await conn.execute(
            text("SELECT count(*) FROM meeting_discourse_segments WHERE segment_id = :sid"),
            {"sid": segment_a.segment_id},
        )
        assert res_seg.scalar() == 1

        res_adr = await conn.execute(
            text("SELECT count(*) FROM extracted_adrs WHERE segment_id = :sid"),
            {"sid": segment_a.segment_id},
        )
        assert res_adr.scalar() == 1

        res_con = await conn.execute(
            text("SELECT count(*) FROM extracted_constraints WHERE segment_id = :sid"),
            {"sid": segment_a.segment_id},
        )
        assert res_con.scalar() == 1

        res_risk = await conn.execute(
            text("SELECT count(*) FROM extracted_risks WHERE segment_id = :sid"),
            {"sid": segment_a.segment_id},
        )
        assert res_risk.scalar() == 1

    # 3. Query as Tenant B -> RLS MUST block access completely (returns 0)
    async with tenant_context(engine, tenant_b) as conn:
        await conn.execute(text("SET LOCAL ROLE rls_test_user"))
        res_seg = await conn.execute(
            text("SELECT count(*) FROM meeting_discourse_segments WHERE segment_id = :sid"),
            {"sid": segment_a.segment_id},
        )
        assert res_seg.scalar() == 0

        res_adr = await conn.execute(
            text("SELECT count(*) FROM extracted_adrs WHERE segment_id = :sid"),
            {"sid": segment_a.segment_id},
        )
        assert res_adr.scalar() == 0

        res_con = await conn.execute(
            text("SELECT count(*) FROM extracted_constraints WHERE segment_id = :sid"),
            {"sid": segment_a.segment_id},
        )
        assert res_con.scalar() == 0

        res_risk = await conn.execute(
            text("SELECT count(*) FROM extracted_risks WHERE segment_id = :sid"),
            {"sid": segment_a.segment_id},
        )
        assert res_risk.scalar() == 0

    await engine.dispose()
