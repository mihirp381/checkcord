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
from checkcord.models.enums import SourcePlatform
from checkcord.models.raw_payloads import RawChatPayload
from checkcord.pipeline.scrubber import PipelineScrubber


def make_raw_payload(
    text: str = "We need to revise the Q3 subscription billing model.",
    author_name: str = "jane_doe",
    bot_id: str | None = None,
    platform: SourcePlatform = SourcePlatform.SLACK,
) -> RawChatPayload:
    """Helper to create a valid RawChatPayload."""
    return RawChatPayload(
        tenant_id=uuid4(),
        platform=platform,
        channel_id="C12345",
        author_id="U9876",
        author_name=author_name,
        message_id="msg_1001",
        happened_at=datetime.now(UTC),
        content_raw=text,
        content_clean=text,
        access_control={"is_public": True},
        raw_event={"bot_id": bot_id} if bot_id else {},
    )


@pytest.mark.asyncio
async def test_llm_client_classify_chat_message_requires_model() -> None:
    with pytest.raises(LLMNotConfiguredError):
        await LLMClient(model=None).classify_chat_message("We must update pricing.")


@pytest.mark.asyncio
async def test_keyword_llm_classify_chat_message() -> None:
    client = KeywordLLMClient()
    # Work message
    assert (
        await client.classify_chat_message(
            "We must update the enterprise subscription tier pricing."
        )
        is True
    )
    # Casual noise
    assert await client.classify_chat_message("hello everyone lol coffee time") is False


@pytest.mark.asyncio
async def test_llm_client_classify_chat_message_langchain() -> None:
    """Verify LLMClient.classify_chat_message uses LangChain model when provided."""
    mock_model = MagicMock(spec=BaseChatModel)
    mock_model.ainvoke = AsyncMock(return_value=AIMessage(content="WORK"))

    client = LLMClient(model=mock_model)
    res = await client.classify_chat_message("Discussion on customer onboarding flow")
    assert res is True
    mock_model.ainvoke.assert_called_once()


@pytest.fixture
def mock_bus() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_llm_client() -> AsyncMock:
    client = AsyncMock(spec=LLMClient)
    client.classify_chat_message.return_value = True
    return client


@pytest.mark.asyncio
@patch("checkcord.pipeline.scrubber.increment_counter")
async def test_process_message_keeps_work_message(
    mock_counter: MagicMock, mock_bus: AsyncMock, mock_llm_client: AsyncMock
) -> None:
    """Verify work messages pass Stage 0 & Stage 1 and are published to events.scrubbed.chat."""
    scrubber = PipelineScrubber(bus=mock_bus, llm_client=mock_llm_client)
    payload = make_raw_payload(text="Discussing SLA requirements for API uptime.")

    await scrubber.process_message(payload)

    # Asserts
    mock_llm_client.classify_chat_message.assert_called_once_with(
        "Discussing SLA requirements for API uptime."
    )
    mock_bus.publish.assert_called_once_with(topic="events.scrubbed.chat", event=payload)
    mock_counter.assert_called_with("chat_scrubber_kept_total", tags={"platform": "slack"})


@pytest.mark.asyncio
@patch("checkcord.pipeline.scrubber.increment_counter")
async def test_process_message_stage0_bot_dropped(
    mock_counter: MagicMock, mock_bus: AsyncMock, mock_llm_client: AsyncMock
) -> None:
    """Verify bot messages are dropped by Stage 0 filter without calling LLM."""
    scrubber = PipelineScrubber(bus=mock_bus, llm_client=mock_llm_client)
    payload = make_raw_payload(author_name="datadog", text="[datadog] alert on pod latency")

    await scrubber.process_message(payload)

    # LLM and publish should NOT be called
    mock_llm_client.classify_chat_message.assert_not_called()
    mock_bus.publish.assert_not_called()
    mock_counter.assert_called_with(
        "chat_scrubber_dropped_total",
        tags={"stage": "stage0_bot_filter", "platform": "slack"},
    )


@pytest.mark.asyncio
@patch("checkcord.pipeline.scrubber.increment_counter")
async def test_process_message_stage1_noise_dropped(
    mock_counter: MagicMock, mock_bus: AsyncMock, mock_llm_client: AsyncMock
) -> None:
    """Verify casual noise messages evaluated as NOISE by Stage 1 are dropped."""
    mock_llm_client.classify_chat_message.return_value = False
    scrubber = PipelineScrubber(bus=mock_bus, llm_client=mock_llm_client)
    payload = make_raw_payload(text="anyone up for lunch?")

    await scrubber.process_message(payload)

    mock_llm_client.classify_chat_message.assert_called_once_with("anyone up for lunch?")
    mock_bus.publish.assert_not_called()
    mock_counter.assert_called_with(
        "chat_scrubber_dropped_total",
        tags={"stage": "stage1_llm_scrubber", "platform": "slack"},
    )


@pytest.mark.asyncio
@patch("checkcord.pipeline.scrubber.increment_counter")
async def test_process_message_invalid_payload_handled(
    mock_counter: MagicMock, mock_bus: AsyncMock, mock_llm_client: AsyncMock
) -> None:
    """Verify malformed payload data records error metric and returns gracefully."""
    scrubber = PipelineScrubber(bus=mock_bus, llm_client=mock_llm_client)

    await scrubber.process_message({"invalid": "payload"})

    mock_bus.publish.assert_not_called()
    mock_counter.assert_called_with("chat_scrubber_error_total", tags={"error": "validation_error"})


@pytest.mark.asyncio
@patch("checkcord.pipeline.scrubber.increment_counter")
async def test_process_message_llm_exception_reraised(
    mock_counter: MagicMock, mock_bus: AsyncMock, mock_llm_client: AsyncMock
) -> None:
    """Verify LLM processing errors emit counter and re-raise for retry handling."""
    mock_llm_client.classify_chat_message.side_effect = RuntimeError("API unavailable")
    scrubber = PipelineScrubber(bus=mock_bus, llm_client=mock_llm_client)
    payload = make_raw_payload(text="Work context")

    with pytest.raises(RuntimeError, match="API unavailable"):
        await scrubber.process_message(payload)

    mock_counter.assert_called_with("chat_scrubber_error_total", tags={"error": "processing_error"})


@pytest.mark.asyncio
async def test_scrubber_shutdown_gracefully(
    mock_bus: AsyncMock, mock_llm_client: AsyncMock
) -> None:
    """Ensure scrubber stop() breaks loop cleanly."""
    scrubber = PipelineScrubber(bus=mock_bus, llm_client=mock_llm_client)

    async def infinite_subscribe(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(10)

    mock_bus.subscribe.side_effect = infinite_subscribe

    task = asyncio.create_task(scrubber.start(["events.raw.chat"]))
    await asyncio.sleep(0.05)
    assert not task.done()

    scrubber.stop()
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()
    mock_bus.subscribe.assert_called_once()
