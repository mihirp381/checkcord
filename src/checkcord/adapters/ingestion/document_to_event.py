"""Converts a unified `schemas.document.Document` (Jira/Notion/Linear) into a `CanonicalEvent`
for publication to the ingestion pipeline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from checkcord.models.common import AccessControl, AuthorInfo, DocumentMetadata
from checkcord.models.enums import AccessVisibility, AclType, SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.schemas.document import Document
from checkcord.services import scope

_PLATFORM_MAP = {
    "jira": SourcePlatform.JIRA,
    "notion": SourcePlatform.NOTION,
    "linear": SourcePlatform.LINEAR,
}

_SOURCE_TYPE_MAP = {
    "jira": SourceType.TICKET_CREATED,
    "notion": SourceType.WIKI_PAGE,
    "linear": SourceType.TASK,
}


def document_to_canonical_event(
    document: Document,
    tenant_id: UUID,
    url: str = "",
    connection: str | None = None,
    container: str | None = None,
) -> CanonicalEvent:
    """Convert a `Document` (from JiraAdapter/NotionAdapter/LinearAdapter) into a `CanonicalEvent`.

    `Document.raw_markdown_fallback` becomes `content_clean`/`content_raw` (it is already the
    "never drop data" flattened representation of the whole block tree); block-level structure
    is preserved in `source_raw_payload` for any downstream consumer that wants it.
    """
    platform = _PLATFORM_MAP.get(document.source_platform)
    if platform is None:
        raise ValueError(f"Unsupported document source_platform: {document.source_platform!r}")
    source_type = _SOURCE_TYPE_MAP[document.source_platform]

    return CanonicalEvent(
        tenant_id=tenant_id,
        source_platform=platform,
        source_type=source_type,
        source_id=document.source_id,
        happened_at=datetime.now(UTC),
        author=AuthorInfo(source_id="unknown", display_name="Unknown Author"),
        content_raw=document.raw_markdown_fallback,
        content_clean=document.raw_markdown_fallback,
        source_raw_payload={
            "document_id": document.id,
            "blocks": [b.model_dump(mode="json") for b in document.blocks],
            "metadata": document.metadata,
        },
        access_control=AccessControl(
            visibility=AccessVisibility.PRIVATE,
            acl_type=AclType.CUSTOM,
            is_public=False,
        ),
        connection=connection,
        audience_container=scope.normalize(container) if container else None,
        document_metadata=DocumentMetadata(
            title=document.title,
            url=url,
            tags=list(document.metadata.get("tags", [])) if document.metadata else [],
        ),
    )
