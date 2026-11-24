# ruff: noqa: E501
"""Stored meeting segments and verified extractions."""

import json
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.extractions import (
    DiscourseSegment,
    ExtractedADR,
    ExtractedConstraint,
    ExtractedRisk,
)
from checkcord.storage.postgres import list_tenant_ids, tenant_context

logger = structlog.get_logger(__name__)


class MeetingExtractionStore:
    """Stores meeting extractions. Callers pass a connection inside tenant_context."""

    async def save_segment_with_extractions(
        self,
        conn: AsyncConnection,
        segment: DiscourseSegment,
        adrs: list[ExtractedADR],
        constraints: list[ExtractedConstraint],
        risks: list[ExtractedRisk],
        connection: str | None = None,
        author: AuthorInfo | None = None,
        access_control: AccessControl | None = None,
    ) -> UUID:
        """Persists a single DiscourseSegment and all associated verified extractions.

        ``author`` and ``access_control`` are the meeting's, kept so a later
        batch verification can publish an extraction with them.
        Runs within the active connection transaction.
        """
        log = logger.bind(
            tenant_id=str(segment.tenant_id),
            segment_id=str(segment.segment_id),
            parent_event_id=str(segment.parent_event_id),
        )

        insert_segment_stmt = text(
            """
            INSERT INTO meeting_discourse_segments (
                segment_id, parent_event_id, tenant_id, start_time, end_time,
                topics, speakers, text_content, confidence_score, connection,
                author, access_control
            ) VALUES (
                :segment_id, :parent_event_id, :tenant_id, :start_time, :end_time,
                :topics, :speakers, :text_content, :confidence_score, :connection,
                :author, :access_control
            )
            """
        )

        await conn.execute(
            insert_segment_stmt,
            {
                "segment_id": segment.segment_id,
                "parent_event_id": segment.parent_event_id,
                "tenant_id": segment.tenant_id,
                "start_time": segment.start_time,
                "end_time": segment.end_time,
                "topics": json.dumps(segment.topics),
                "speakers": json.dumps(segment.speakers),
                "text_content": segment.text_content,
                "confidence_score": segment.confidence_score,
                "connection": connection,
                "author": author.model_dump_json() if author else None,
                "access_control": access_control.model_dump_json() if access_control else None,
            },
        )

        if adrs:
            insert_adr_stmt = text(
                """
                INSERT INTO extracted_adrs (
                    id, segment_id, tenant_id, decision_title, context,
                    alternatives_considered, final_decision, primary_driver, audit_status
                ) VALUES (
                    :id, :segment_id, :tenant_id, :decision_title, :context,
                    :alternatives_considered, :final_decision, :primary_driver, :audit_status
                )
                """
            )
            adr_params = [
                {
                    "id": adr.id,
                    "segment_id": segment.segment_id,
                    "tenant_id": segment.tenant_id,
                    "decision_title": adr.decision_title,
                    "context": adr.context,
                    "alternatives_considered": json.dumps(adr.alternatives_considered),
                    "final_decision": adr.final_decision,
                    "primary_driver": adr.primary_driver,
                    "audit_status": adr.audit_status.value,
                }
                for adr in adrs
            ]
            await conn.execute(insert_adr_stmt, adr_params)

        if constraints:
            insert_constraint_stmt = text(
                """
                INSERT INTO extracted_constraints (
                    id, segment_id, tenant_id, constraint_type, description, source_quote, audit_status
                ) VALUES (
                    :id, :segment_id, :tenant_id, :constraint_type, :description, :source_quote, :audit_status
                )
                """
            )
            constraint_params = [
                {
                    "id": constraint.id,
                    "segment_id": segment.segment_id,
                    "tenant_id": segment.tenant_id,
                    "constraint_type": constraint.constraint_type.value,
                    "description": constraint.description,
                    "source_quote": constraint.source_quote,
                    "audit_status": constraint.audit_status.value,
                }
                for constraint in constraints
            ]
            await conn.execute(insert_constraint_stmt, constraint_params)

        if risks:
            insert_risk_stmt = text(
                """
                INSERT INTO extracted_risks (
                    id, segment_id, tenant_id, risk_level, description,
                    potential_mitigation, deferred_action_items, audit_status
                ) VALUES (
                    :id, :segment_id, :tenant_id, :risk_level, :description,
                    :potential_mitigation, :deferred_action_items, :audit_status
                )
                """
            )
            risk_params = [
                {
                    "id": risk.id,
                    "segment_id": segment.segment_id,
                    "tenant_id": segment.tenant_id,
                    "risk_level": risk.risk_level.value,
                    "description": risk.description,
                    "potential_mitigation": risk.potential_mitigation,
                    "deferred_action_items": json.dumps(risk.deferred_action_items),
                    "audit_status": risk.audit_status.value,
                }
                for risk in risks
            ]
            await conn.execute(insert_risk_stmt, risk_params)

        log.info(
            "meeting_extractions.saved_successfully",
            adr_count=len(adrs),
            constraint_count=len(constraints),
            risk_count=len(risks),
        )

        return segment.segment_id  # type: ignore[no-any-return]

    async def get_pending_extractions(
        self, conn: AsyncConnection, limit: int = 100
    ) -> (
        list[dict[str, float | str | list[str] | dict[str, str | float | list[str]]]]
        | list[dict[str, str | float | list[str] | dict[str, str | float | list[str]]]]
    ):
        """Fetch pending extractions across ADRs, Constraints, and Risks along with segment text."""
        # author and access_control are the meeting's; the verifier
        # republishes a verified extraction with them.
        query = text(
            """
            SELECT
                a.id as extraction_id, 'adr' as entity_type, a.tenant_id, s.text_content,
                s.parent_event_id, s.connection, s.author, s.access_control,
                json_build_object(
                    'decision_title', a.decision_title,
                    'context', a.context,
                    'alternatives_considered', a.alternatives_considered,
                    'final_decision', a.final_decision,
                    'primary_driver', a.primary_driver
                ) as payload
            FROM extracted_adrs a
            JOIN meeting_discourse_segments s ON a.segment_id = s.segment_id
            WHERE a.audit_status = 'pending_audit'

            UNION ALL

            SELECT
                c.id as extraction_id, 'constraint' as entity_type, c.tenant_id, s.text_content,
                s.parent_event_id, s.connection, s.author, s.access_control,
                json_build_object(
                    'constraint_type', c.constraint_type,
                    'description', c.description,
                    'source_quote', c.source_quote
                ) as payload
            FROM extracted_constraints c
            JOIN meeting_discourse_segments s ON c.segment_id = s.segment_id
            WHERE c.audit_status = 'pending_audit'

            UNION ALL

            SELECT
                r.id as extraction_id, 'risk' as entity_type, r.tenant_id, s.text_content,
                s.parent_event_id, s.connection, s.author, s.access_control,
                json_build_object(
                    'risk_level', r.risk_level,
                    'description', r.description,
                    'potential_mitigation', r.potential_mitigation,
                    'deferred_action_items', r.deferred_action_items
                ) as payload
            FROM extracted_risks r
            JOIN meeting_discourse_segments s ON r.segment_id = s.segment_id
            WHERE r.audit_status = 'pending_audit'
            LIMIT :limit;
            """
        )
        res = await conn.execute(query, {"limit": limit})
        rows = res.mappings().all()
        return [dict(r) for r in rows]

    async def update_extraction_audit_statuses(
        self,
        conn: AsyncConnection,
        verified_ids: list[UUID],
        rejected_ids: list[UUID],
    ) -> None:
        """Update audit_status for verified and rejected extraction IDs across all 3 tables."""
        tables = ["extracted_adrs", "extracted_constraints", "extracted_risks"]

        if verified_ids:
            for table in tables:
                stmt = text(f"UPDATE {table} SET audit_status = 'verified' WHERE id = ANY(:ids)")
                await conn.execute(stmt, {"ids": verified_ids})

        if rejected_ids:
            for table in tables:
                stmt = text(f"UPDATE {table} SET audit_status = 'rejected' WHERE id = ANY(:ids)")
                await conn.execute(stmt, {"ids": rejected_ids})

    async def get_tenants_with_pending_extractions(self, engine: AsyncEngine) -> list[UUID]:
        """Return the known tenants that have at least one pending_audit extraction."""
        query = text(
            """
            SELECT EXISTS (SELECT 1 FROM extracted_adrs
                           WHERE tenant_id = :t AND audit_status = 'pending_audit')
                OR EXISTS (SELECT 1 FROM extracted_constraints
                           WHERE tenant_id = :t AND audit_status = 'pending_audit')
                OR EXISTS (SELECT 1 FROM extracted_risks
                           WHERE tenant_id = :t AND audit_status = 'pending_audit')
            """
        )
        pending: list[UUID] = []
        for tenant_id in await list_tenant_ids(engine):
            async with tenant_context(engine, tenant_id) as conn:
                if await conn.scalar(query, {"t": tenant_id}):
                    pending.append(tenant_id)
        return pending
