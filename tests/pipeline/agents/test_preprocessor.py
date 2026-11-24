from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.pipeline.agents.preprocessor import (
    LLMSegment,
    SegmentationOutput,
    deterministic_chunk_text,
    segment_transcript,
)


def make_dummy_event(content: str) -> CanonicalEvent:
    tenant_id = uuid4()
    return CanonicalEvent(
        tenant_id=tenant_id,
        source_platform=SourcePlatform.ZOOM,
        source_type=SourceType.MEETING_TRANSCRIPT,
        source_id="mtg_123",
        happened_at=datetime.now(UTC),
        author=AuthorInfo(source_id="user_1"),
        content_raw=content,
        content_clean=content,
        access_control=AccessControl(is_public=True),
    )


def test_deterministic_chunk_text_empty() -> None:
    assert deterministic_chunk_text("") == []
    assert deterministic_chunk_text("   ") == []


def test_deterministic_chunk_text_small() -> None:
    text = "Hello world this is a short text"
    chunks = deterministic_chunk_text(text, chunk_size=10, overlap=2)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_deterministic_chunk_text_large() -> None:
    words = [f"word_{i}" for i in range(120)]
    text = " ".join(words)
    chunks = deterministic_chunk_text(text, chunk_size=50, overlap=10)

    # 120 words with chunk_size 50 and step 40 -> 0-50, 40-90, 80-120 -> 3 chunks
    assert len(chunks) == 3

    chunk0_words = chunks[0].split()
    chunk1_words = chunks[1].split()
    assert len(chunk0_words) == 50
    assert len(chunk1_words) == 50
    # Verify 10-word overlap between chunk0 and chunk1
    assert chunk0_words[40:] == chunk1_words[:10]


@pytest.mark.asyncio
async def test_segment_transcript_empty() -> None:
    event = make_dummy_event("")
    segments = await segment_transcript(event)
    assert segments == []


@pytest.mark.asyncio
async def test_segment_transcript_llm_success() -> None:
    event = make_dummy_event("Alice: Let's discuss backend architecture.\nBob: I agree.")

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.return_value = SegmentationOutput(
        segments=[
            LLMSegment(
                start_time=0.0,
                end_time=30.0,
                topics=["architecture"],
                speakers=["Alice", "Bob"],
                text_content="Alice: Let's discuss backend architecture.\nBob: I agree.",
            )
        ]
    )

    mock_llm_client = MagicMock()
    mock_llm_client.with_structured_output.return_value = mock_structured_llm

    segments = await segment_transcript(event, llm_client=mock_llm_client)

    assert len(segments) == 1
    assert segments[0].parent_event_id == event.event_id
    assert segments[0].tenant_id == event.tenant_id
    assert segments[0].topics == ["architecture"]
    assert segments[0].confidence_score == 1.0


@pytest.mark.asyncio
async def test_segment_transcript_retries_once_then_succeeds() -> None:
    """A first-attempt validation failure is not fatal: segment_transcript
    retries and returns the LLM segmentation, not the deterministic chunker's.
    """
    event = make_dummy_event("Alice: Let's discuss backend architecture.\nBob: I agree.")

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.side_effect = [
        ValueError("schema validation failed: invalid output"),
        SegmentationOutput(
            segments=[
                LLMSegment(
                    start_time=0.0,
                    end_time=30.0,
                    topics=["architecture"],
                    speakers=["Alice", "Bob"],
                    text_content="Alice: Let's discuss backend architecture.\nBob: I agree.",
                )
            ]
        ),
    ]

    mock_llm_client = MagicMock()
    mock_llm_client.with_structured_output.return_value = mock_structured_llm

    segments = await segment_transcript(event, llm_client=mock_llm_client)

    assert len(segments) == 1
    assert segments[0].confidence_score == 1.0  # LLM path, not the 0.0 fallback path
    assert mock_structured_llm.ainvoke.call_count == 2


@pytest.mark.asyncio
async def test_segment_transcript_llm_error_fallback() -> None:
    long_content = " ".join([f"token_{i}" for i in range(600)])
    event = make_dummy_event(long_content)

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.side_effect = RuntimeError("Rate limit / API error")

    mock_llm_client = MagicMock()
    mock_llm_client.with_structured_output.return_value = mock_structured_llm

    segments = await segment_transcript(event, llm_client=mock_llm_client)

    assert len(segments) > 1
    for seg in segments:
        assert seg.parent_event_id == event.event_id
        assert seg.tenant_id == event.tenant_id
        assert seg.topics == ["fallback_chunk"]
        assert seg.confidence_score == 0.0
