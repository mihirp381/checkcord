import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from keyword_llm import KeywordLLMClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage

from checkcord.llm.client import LLMClient, LLMNotConfiguredError
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.pipeline.skill_synthesizer import (
    SkillSynthesisWorker,
    parse_skills_from_markdown,
)


def test_parse_skills_from_markdown_single() -> None:
    """Verify parsing a single skill from LLM markdown output."""
    markdown = """
---
name: Synthesize Database Routes
description: Guide on adding API endpoints for postgres
---
# Instructions
Use standard endpoints and tenant isolation context.
"""
    skills = parse_skills_from_markdown(markdown)
    assert len(skills) == 1
    assert skills[0]["name"] == "Synthesize Database Routes"
    assert skills[0]["description"] == "Guide on adding API endpoints for postgres"
    assert "# Instructions" in skills[0]["instructions"]
    assert "---" in skills[0]["instructions"]


def test_parse_skills_from_markdown_multiple() -> None:
    """Verify parsing multiple skills from LLM markdown output."""
    markdown = """
---
name: Skill A
description: Desc A
---
Body A
---
name: Skill B
description: Desc B
---
Body B
"""
    skills = parse_skills_from_markdown(markdown)
    assert len(skills) == 2
    assert skills[0]["name"] == "Skill A"
    assert skills[0]["description"] == "Desc A"
    assert skills[1]["name"] == "Skill B"
    assert skills[1]["description"] == "Desc B"


def test_parse_skills_from_markdown_malformed() -> None:
    """Verify parsing handles malformed/empty input gracefully."""
    assert parse_skills_from_markdown("") == []
    assert parse_skills_from_markdown("no boundaries here") == []


@pytest.mark.asyncio
async def test_llm_client_synthesize_skills_requires_model() -> None:
    with pytest.raises(LLMNotConfiguredError):
        await LLMClient(model=None).synthesize_skills("Fix login bug using bearer token.")


@pytest.mark.asyncio
async def test_keyword_llm_synthesize_skills() -> None:
    client = KeywordLLMClient()
    res = await client.synthesize_skills("Fix login bug using bearer token.")
    assert "Mock Auth Skill" in res


@pytest.mark.asyncio
async def test_llm_client_langchain_invoke() -> None:
    """Verify LLMClient invokes LangChain chat model correctly."""
    mock_model = MagicMock(spec=BaseChatModel)
    mock_model.ainvoke = AsyncMock(
        return_value=AIMessage(content="---\nname: LC Skill\ndescription: LC Desc\n---\nLC Body")
    )

    client = LLMClient(model=mock_model)
    res = await client.synthesize_skills("trajectory input")

    assert "LC Skill" in res
    mock_model.ainvoke.assert_called_once()


@pytest.mark.asyncio
async def test_llm_client_judge_linkage_requires_model() -> None:
    with pytest.raises(LLMNotConfiguredError):
        await LLMClient(model=None).judge_linkage("summary", "This is the same!")


@pytest.mark.asyncio
async def test_keyword_llm_judge_linkage() -> None:
    client = KeywordLLMClient()
    assert await client.judge_linkage("Some context summary", "This is the same!") == "same"
    assert await client.judge_linkage("Some context summary", "Other content") == "different"


@pytest.mark.asyncio
async def test_llm_client_judge_linkage_langchain() -> None:
    mock_model = MagicMock(spec=BaseChatModel)
    mock_model.ainvoke = AsyncMock(return_value=AIMessage(content="SAME"))

    client = LLMClient(model=mock_model)
    res = await client.judge_linkage("Existing context summary", "Raw event content")
    assert res == "same"
    mock_model.ainvoke.assert_called_once()


@pytest.fixture
def mock_bus() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_engine() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_llm_client() -> AsyncMock:
    client = AsyncMock(spec=LLMClient)
    client.synthesize_skills.return_value = (
        "---\n"
        "name: Synthesized Auth\n"
        "description: Guide for auth\n"
        "---\n"
        "# Auth Guide\n"
        "Verify all bearer tokens."
    )
    return client


def make_agent_event(
    source_platform: SourcePlatform = SourcePlatform.AGENT_SESSION,
    source_type: SourceType = SourceType.AGENT_LOG,
    metadata: dict[str, Any] | None = None,
) -> CanonicalEvent:
    """Helper to construct a valid CanonicalEvent."""
    return CanonicalEvent(
        tenant_id=uuid4(),
        source_platform=source_platform,
        source_type=source_type,
        source_id="sess_123",
        happened_at=datetime.now(UTC),
        author={"source_id": "agent_007", "display_name": "Agent 007"},
        content_raw="raw agent trajectory content",
        content_clean="clean agent trajectory content",
        access_control={"is_public": False},
        metadata=metadata or {"status": "success"},
    )


@pytest.mark.asyncio
async def test_worker_shutdown_gracefully(
    mock_bus: AsyncMock, mock_engine: AsyncMock, mock_llm_client: AsyncMock
) -> None:
    """Ensure stop() breaks the loop and cancels subscriptions safely."""
    worker = SkillSynthesisWorker(mock_bus, mock_engine, mock_llm_client)

    async def infinite_subscribe(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(10)

    mock_bus.subscribe.side_effect = infinite_subscribe

    task = asyncio.create_task(worker.start(["topic.test"]))
    await asyncio.sleep(0.05)
    assert not task.done()

    worker.stop()
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()
    mock_bus.subscribe.assert_called_once()


@pytest.mark.asyncio
@patch("checkcord.pipeline.skill_synthesizer.tenant_context")
@patch("checkcord.pipeline.skill_synthesizer.PostgresEventStore")
async def test_process_message_filters_non_agent_events(
    mock_store_cls: AsyncMock,
    mock_tenant_context: AsyncMock,
    mock_bus: AsyncMock,
    mock_engine: AsyncMock,
    mock_llm_client: AsyncMock,
) -> None:
    """Verify non-agent events are ignored."""
    event = make_agent_event(source_platform=SourcePlatform.SLACK, source_type=SourceType.MESSAGE)
    worker = SkillSynthesisWorker(mock_bus, mock_engine, mock_llm_client)

    await worker.process_message(event)

    mock_llm_client.synthesize_skills.assert_not_called()
    mock_tenant_context.assert_not_called()


@pytest.mark.asyncio
@patch("checkcord.pipeline.skill_synthesizer.tenant_context")
@patch("checkcord.pipeline.skill_synthesizer.PostgresEventStore")
async def test_process_message_filters_non_success_trajectories(
    mock_store_cls: AsyncMock,
    mock_tenant_context: AsyncMock,
    mock_bus: AsyncMock,
    mock_engine: AsyncMock,
    mock_llm_client: AsyncMock,
) -> None:
    """Verify failed/non-success trajectories are ignored."""
    event = make_agent_event(metadata={"status": "failed"})
    worker = SkillSynthesisWorker(mock_bus, mock_engine, mock_llm_client)

    await worker.process_message(event)

    mock_llm_client.synthesize_skills.assert_not_called()
    mock_tenant_context.assert_not_called()


@pytest.mark.asyncio
@patch("checkcord.pipeline.skill_synthesizer.tenant_context")
@patch("checkcord.pipeline.skill_synthesizer.PostgresEventStore")
async def test_process_message_synthesizes_and_saves_skills(
    mock_store_cls: AsyncMock,
    mock_tenant_context: AsyncMock,
    mock_bus: AsyncMock,
    mock_engine: AsyncMock,
    mock_llm_client: AsyncMock,
) -> None:
    """Verify successful synthesis and storage of skills and event metadata updates."""
    # Setup context manager mocks
    mock_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_conn
    mock_tenant_context.return_value = mock_context

    # Setup store mocks
    mock_store_instance = AsyncMock()
    mock_store_cls.return_value = mock_store_instance

    event = make_agent_event(
        metadata={
            "status": "success",
            "prompt": "write a router",
            "tool_calls": [{"tool_name": "view_file"}],
            "files_read": ["src/main.py"],
            "tokens_used": 1500,
        }
    )

    worker = SkillSynthesisWorker(mock_bus, mock_engine, mock_llm_client)
    await worker.process_message(event)

    # Asserts
    mock_llm_client.synthesize_skills.assert_called_once_with(event.content_clean)
    mock_tenant_context.assert_called_once_with(mock_engine, event.tenant_id)

    # Assert Skills table insert was executed (since we mock DBSkill.__table__)
    # Note: conn.execute is called for skills insert
    assert mock_conn.execute.call_count == 1

    # Assert event store update_agent_metadata was called
    mock_store_instance.update_agent_metadata.assert_called_once()
    updated_meta = mock_store_instance.update_agent_metadata.call_args.kwargs["agent_metadata"]
    assert updated_meta.session_id == "sess_123"
    assert updated_meta.prompt == "write a router"
    assert updated_meta.synthesized_skills == ["Synthesized Auth"]
    assert updated_meta.tokens_used == 1500
