"""Publish a verified meeting extraction (ADR, constraint or risk) as a CanonicalEvent.

The event goes onto ``events.raw.custom`` and takes the same save, embed,
cluster and link path as every other event, so dedup, scope assignment and
the audit trail apply. ``metadata.context_type`` is read by the linkage
worker when it creates a new record. ``access_control`` and ``author`` are
inherited from the originating meeting event.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from checkcord.bus.interface import EventBus
from checkcord.llm.outcomes import _detail_kept
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent


def format_adr_content(final_decision: str, context: str, source: str) -> str:
    if context and context.strip() and _detail_kept(context, source):
        return f"{final_decision}\n- {context.strip()}"
    return final_decision


def format_constraint_content(description: str) -> str:
    return description


def format_risk_content(description: str) -> str:
    return description


async def publish_extraction_as_context_candidate(
    bus: EventBus,
    *,
    tenant_id: UUID,
    context_type: str,
    content: str,
    author: AuthorInfo,
    access_control: AccessControl,
    source_id: str | None = None,
    connection: str | None = None,
) -> None:
    """Publish one verified extraction onto ``events.raw.custom``.

    ``context_type`` is ``"adr"``, ``"constraint"`` or ``"risk"``.
    ``source_id`` defaults to a fresh UUID string.
    """
    event = CanonicalEvent(
        tenant_id=tenant_id,
        source_platform=SourcePlatform.CUSTOM,
        source_type=SourceType.CUSTOM_PAYLOAD,
        source_id=source_id or str(uuid4()),
        happened_at=datetime.now(UTC),
        author=author,
        content_raw=content,
        content_clean=content,
        access_control=access_control,
        connection=connection,
        metadata={"context_type": context_type},
    )
    await bus.publish("events.raw.custom", event)
