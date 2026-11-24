# ruff: noqa: E501
from typing import Any, cast

import structlog
from pydantic import BaseModel, Field

from checkcord.core.config import get_settings
from checkcord.core.llm_retry import ainvoke_with_retry
from checkcord.core.rate_limiter import global_rate_limiter
from checkcord.llm.factory import build_chat_model
from checkcord.models.event import CanonicalEvent
from checkcord.models.extractions import DiscourseSegment

logger = structlog.get_logger(__name__)


def deterministic_chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """Fallback deterministic sliding-window chunker based on word counts.

    Splits text into chunks of `chunk_size` words with `overlap` words between adjacent chunks.
    """
    if not text or not text.strip():
        return []

    words = text.split()
    if len(words) <= chunk_size:
        return [text]

    chunks: list[str] = []
    step = chunk_size - overlap
    if step <= 0:
        step = 1

    for start_idx in range(0, len(words), step):
        end_idx = min(start_idx + chunk_size, len(words))
        chunk_words = words[start_idx:end_idx]
        chunks.append(" ".join(chunk_words))
        if end_idx == len(words):
            break

    return chunks


class LLMSegment(BaseModel):
    start_time: float = 0.0
    end_time: float = 0.0
    topics: list[str] = Field(default_factory=list)
    speakers: list[str] = Field(default_factory=list)
    text_content: str


class SegmentationOutput(BaseModel):
    segments: list[LLMSegment] = Field(default_factory=list)


SYSTEM_PROMPT = """You are an expert transcript preprocessor and discourse analyst.
Your task is to analyze raw speech-to-text meeting transcripts and segment them into coherent topical discourse blocks.
Ensure that each segment covers a single primary topic to prevent cross-topic context bleeding.
Identify speaker names and timestamps where available. Do NOT modify or summarize the raw text content inside each segment; keep original speech utterances verbatim.
"""


async def segment_transcript(
    event: CanonicalEvent, llm_client: Any | None = None
) -> list[DiscourseSegment]:
    """Segments a meeting transcript CanonicalEvent into a list of DiscourseSegments.

    Attempts topic-based segmentation via LLM (gpt-4o-mini).
    Falls back to deterministic sliding-window chunking on error.
    """
    log = logger.bind(
        tenant_id=str(event.tenant_id),
        event_id=str(event.event_id),
    )

    if not event.content_raw or not event.content_raw.strip():
        log.warning("pipeline.preprocessor.empty_content")
        return []

    try:
        if llm_client is None:
            llm_client = build_chat_model(
                get_settings().meeting_extraction_llm_model, purpose="meeting_segmentation"
            )

        structured_llm = llm_client.with_structured_output(SegmentationOutput)

        user_prompt = f"Transcript Content:\n{event.content_raw}"
        messages = [
            ("system", SYSTEM_PROMPT),
            ("user", user_prompt),
        ]

        res = cast(
            SegmentationOutput,
            await ainvoke_with_retry(
                structured_llm,
                messages,
                agent="PreprocessorAgent",
                before_attempt=lambda: global_rate_limiter.acquire(1000),
            ),
        )

        if res and res.segments:
            discourse_segments: list[DiscourseSegment] = []
            for seg in res.segments:
                discourse_segments.append(
                    DiscourseSegment(
                        parent_event_id=event.event_id,
                        tenant_id=event.tenant_id,
                        start_time=seg.start_time,
                        end_time=seg.end_time,
                        topics=seg.topics,
                        speakers=seg.speakers,
                        text_content=seg.text_content,
                        confidence_score=1.0,
                    )
                )
            log.info("pipeline.preprocessor.llm_success", segment_count=len(discourse_segments))
            return discourse_segments
        else:
            log.warning("pipeline.preprocessor.llm_empty_output")

    except Exception as e:
        log.warning("pipeline.preprocessor.llm_error_fallback", error=str(e))

    # Fallback path: Deterministic sliding-window chunker
    raw_chunks = deterministic_chunk_text(event.content_raw, chunk_size=500, overlap=50)
    fallback_segments: list[DiscourseSegment] = []
    for idx, chunk_text in enumerate(raw_chunks):
        fallback_segments.append(
            DiscourseSegment(
                parent_event_id=event.event_id,
                tenant_id=event.tenant_id,
                start_time=float(idx * 60),
                end_time=float((idx + 1) * 60),
                topics=["fallback_chunk"],
                speakers=[],
                text_content=chunk_text,
                confidence_score=0.0,
            )
        )

    log.info("pipeline.preprocessor.fallback_success", segment_count=len(fallback_segments))
    return fallback_segments
