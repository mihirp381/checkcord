from uuid import uuid4

import pytest

from checkcord.adapters.ingestion.document_to_event import document_to_canonical_event
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.schemas.document import Document, DocumentBlock


def test_jira_document_maps_to_ticket_created() -> None:
    tenant_id = uuid4()
    doc = Document(
        title="[PROJ-1] Fix the bug",
        source_platform="jira",
        source_id="PROJ-1",
        blocks=[DocumentBlock(type="paragraph", text="We decided to use Postgres.")],
        raw_markdown_fallback="# PROJ-1: Fix the bug\n\nWe decided to use Postgres.",
    )

    event = document_to_canonical_event(doc, tenant_id, url="https://acme.atlassian.net/PROJ-1")

    assert event.tenant_id == tenant_id
    assert event.source_platform == SourcePlatform.JIRA
    assert event.source_type == SourceType.TICKET_CREATED
    assert event.source_id == "PROJ-1"
    assert event.content_clean == doc.raw_markdown_fallback
    assert event.content_raw == doc.raw_markdown_fallback
    assert event.document_metadata is not None
    assert event.document_metadata.title == "[PROJ-1] Fix the bug"
    assert event.document_metadata.url == "https://acme.atlassian.net/PROJ-1"
    assert event.source_raw_payload["document_id"] == doc.id
    assert len(event.source_raw_payload["blocks"]) == 1


def test_notion_document_maps_to_wiki_page() -> None:
    tenant_id = uuid4()
    doc = Document(
        title="Architecture Notes",
        source_platform="notion",
        source_id="page_123",
        raw_markdown_fallback="# Architecture Notes",
    )

    event = document_to_canonical_event(doc, tenant_id)

    assert event.source_platform == SourcePlatform.NOTION
    assert event.source_type == SourceType.WIKI_PAGE


def test_linear_document_maps_to_task() -> None:
    tenant_id = uuid4()
    doc = Document(
        title="[ENG-1] Refactor auth",
        source_platform="linear",
        source_id="ENG-1",
        raw_markdown_fallback="# ENG-1",
    )

    event = document_to_canonical_event(doc, tenant_id)

    assert event.source_platform == SourcePlatform.LINEAR
    assert event.source_type == SourceType.TASK


def test_document_metadata_tags_are_preserved() -> None:
    tenant_id = uuid4()
    doc = Document(
        title="Tagged doc",
        source_platform="notion",
        source_id="page_456",
        raw_markdown_fallback="content",
        metadata={"tags": ["architecture", "security"]},
    )

    event = document_to_canonical_event(doc, tenant_id)

    assert event.document_metadata is not None
    assert event.document_metadata.tags == ["architecture", "security"]


def test_unsupported_platform_raises() -> None:
    tenant_id = uuid4()
    doc = Document(
        title="x",
        source_platform="confluence",
        source_id="1",
        raw_markdown_fallback="x",
    )

    with pytest.raises(ValueError, match="Unsupported document source_platform"):
        document_to_canonical_event(doc, tenant_id)
