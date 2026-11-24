"""Tests for pipeline/agents/extraction_publisher.py. The event bus is mocked;
no LLM or embedding calls.
"""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.pipeline.agents.extraction_publisher import (
    format_adr_content,
    format_constraint_content,
    format_risk_content,
    publish_extraction_as_context_candidate,
)


def test_format_adr_content_is_the_decision_and_supported_context() -> None:
    source = "Alice: We chose Postgres over MongoDB for multi-tenant isolation."
    content = format_adr_content(
        final_decision="We chose Postgres over MongoDB.",
        context="Multi-tenant isolation",
        source=source,
    )
    assert content == "We chose Postgres over MongoDB.\n- Multi-tenant isolation"
    assert format_adr_content("We chose Postgres.", "Cost savings", source) == "We chose Postgres."


def test_format_constraint_and_risk_content_are_the_description() -> None:
    assert format_constraint_content("GDPR mandates user deletion.") == (
        "GDPR mandates user deletion."
    )
    assert format_risk_content("Unencrypted payload risk") == "Unencrypted payload risk"


@pytest.mark.asyncio
async def test_publish_extraction_as_context_candidate_publishes_to_events_raw_custom() -> None:
    mock_bus = AsyncMock()
    tenant_id = uuid4()
    author = AuthorInfo(source_id="host_1")
    access_control = AccessControl(is_public=True)

    await publish_extraction_as_context_candidate(
        mock_bus,
        tenant_id=tenant_id,
        context_type="risk",
        content="Risk (high): Unencrypted payload risk",
        author=author,
        access_control=access_control,
    )

    mock_bus.publish.assert_called_once()
    topic, event = mock_bus.publish.call_args.args
    assert topic == "events.raw.custom"
    assert event.tenant_id == tenant_id
    assert event.source_platform == SourcePlatform.CUSTOM
    assert event.source_type == SourceType.CUSTOM_PAYLOAD
    assert event.content_clean == "Risk (high): Unencrypted payload risk"
    assert event.content_raw == event.content_clean
    assert event.author == author
    assert event.access_control == access_control
    assert event.metadata == {"context_type": "risk"}


@pytest.mark.asyncio
async def test_publish_extraction_as_context_candidate_default_source_id_is_unique() -> None:
    mock_bus = AsyncMock()
    tenant_id = uuid4()
    author = AuthorInfo(source_id="host_1")
    access_control = AccessControl()

    await publish_extraction_as_context_candidate(
        mock_bus,
        tenant_id=tenant_id,
        context_type="adr",
        content="Decision: use Postgres.",
        author=author,
        access_control=access_control,
    )
    await publish_extraction_as_context_candidate(
        mock_bus,
        tenant_id=tenant_id,
        context_type="adr",
        content="Decision: use Postgres.",
        author=author,
        access_control=access_control,
    )

    first_event = mock_bus.publish.call_args_list[0].args[1]
    second_event = mock_bus.publish.call_args_list[1].args[1]
    assert first_event.source_id != second_event.source_id


@pytest.mark.asyncio
async def test_publish_extraction_as_context_candidate_uses_explicit_source_id() -> None:
    mock_bus = AsyncMock()
    tenant_id = uuid4()

    await publish_extraction_as_context_candidate(
        mock_bus,
        tenant_id=tenant_id,
        context_type="constraint",
        content="Must run on Postgres.",
        author=AuthorInfo(source_id="host_1"),
        access_control=AccessControl(),
        source_id="extraction-constraint-abc123",
    )

    event = mock_bus.publish.call_args.args[1]
    assert event.source_id == "extraction-constraint-abc123"
