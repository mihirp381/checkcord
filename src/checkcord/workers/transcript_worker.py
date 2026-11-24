"""Meeting transcripts to segments, extractions checked by the verifier, and stored records."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.bus.interface import EventBus
from checkcord.core.config import get_settings
from checkcord.core.telemetry import increment_counter
from checkcord.llm.outcomes import content_words, statement_supported
from checkcord.models.enums import SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.models.extractions import (
    DiscourseSegment,
    ExtractedADR,
    ExtractedConstraint,
    ExtractedRisk,
)
from checkcord.pipeline.agents.extraction_publisher import (
    format_adr_content,
    format_constraint_content,
    format_risk_content,
    publish_extraction_as_context_candidate,
)
from checkcord.pipeline.agents.judge import judge_payload, kept_detail, verify_extraction
from checkcord.pipeline.agents.preprocessor import segment_transcript
from checkcord.pipeline.agents.specialists import (
    extract_adr,
    extract_constraints,
    extract_risks,
)
from checkcord.storage.meeting_extractions import MeetingExtractionStore
from checkcord.storage.postgres import tenant_context

logger = structlog.get_logger(__name__)

SENTENCE_SHARE = 0.5
DUPLICATE_OVERLAP = 0.6
_TIMESTAMP = re.compile(r"^\[[\d:.]+\]\s*")
_SPEAKER = re.compile(r"^(?:[A-Z][\w.'-]*(?: [A-Z][\w.'-]*){0,3}|[\w.+-]+@[\w.-]+):\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"\w+")


def _sentences(source: str) -> list[str]:
    out: list[str] = []
    for line in source.splitlines():
        line = _SPEAKER.sub("", _TIMESTAMP.sub("", line.strip()), count=1)
        out.extend(s.strip() for s in _SENTENCE_END.split(line) if s.strip())
    return out


def supporting_sentence(statement: str, source: str) -> str | None:
    words = content_words(statement)
    if not words:
        return None
    tokens = set(_WORD.findall(statement.lower()))
    best, key = None, (0.0, 0)
    for sentence in _sentences(source):
        share = len(words & content_words(sentence)) / len(words)
        rank = (share, len(tokens & set(_WORD.findall(sentence.lower()))))
        if rank > key:
            best, key = sentence, rank
    return best if key[0] >= SENTENCE_SHARE else None


def overlap(a: str, b: str) -> float:
    wa, wb = content_words(a), content_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def one_per_statement(
    adrs: list[ExtractedADR], constraints: list[ExtractedConstraint], risks: list[ExtractedRisk]
) -> tuple[list[ExtractedADR], list[ExtractedConstraint], list[ExtractedRisk]]:
    kept: list[str] = []

    def keep(statement: str) -> bool:
        if any(overlap(statement, k) >= DUPLICATE_OVERLAP for k in kept):
            increment_counter("meeting_extraction_duplicate_total")
            return False
        kept.append(statement)
        return True

    return (
        [a for a in adrs if keep(a.final_decision)],
        [c for c in constraints if keep(c.description)],
        [r for r in risks if keep(r.description)],
    )


class TranscriptExtractionWorker:
    """Segments meeting transcripts, extracts records, verifies them and stores them."""

    def __init__(
        self,
        bus: EventBus,
        engine: AsyncEngine | None = None,
        group_name: str = "meeting_extractors",
        consumer_name: str = "transcript-worker-1",
        llm_client: Any | None = None,
        max_concurrency: int = 10,
        sync_judge: bool | None = None,
        settled_judge: Any | None = None,
    ) -> None:
        self.bus = bus
        self.engine = engine
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.llm_client = llm_client
        self.settled_judge = settled_judge or llm_client
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.stop_event = asyncio.Event()
        self.store = MeetingExtractionStore()
        self.sync_judge = (
            get_settings().meeting_judge_mode == "direct" if sync_judge is None else sync_judge
        )

        # In-memory storage for test inspection / verification
        self.processed_segments: list[DiscourseSegment] = []
        self.extracted_adrs: list[ExtractedADR] = []
        self.extracted_constraints: list[ExtractedConstraint] = []
        self.extracted_risks: list[ExtractedRisk] = []
        self.rejected_extractions_count: int = 0

    async def process_message(self, event: CanonicalEvent) -> None:
        """Segment a transcript, run the extractors in parallel and store the extractions.

        Extractions the segment does not support are dropped. The rest are stored pending
        verification, or verified now when sync_judge is True.
        """
        log = logger.bind(
            tenant_id=str(event.tenant_id),
            event_id=str(event.event_id),
            source_type=event.source_type.value,
        )

        if event.source_type != SourceType.MEETING_TRANSCRIPT:
            log.debug("transcript_worker.skipped_non_meeting_event")
            return

        try:
            # Preprocessing and segmentation
            segments = await segment_transcript(event, llm_client=self.llm_client)
            self.processed_segments.extend(segments)
            log.info(
                "pipeline.transcript_worker.segmented_successfully",
                segment_count=len(segments),
            )

            if not segments:
                return

            for seg in segments:
                results = await asyncio.gather(
                    self._guarded_extract_adr(seg),
                    self._guarded_extract_constraints(seg),
                    self._guarded_extract_risks(seg),
                    return_exceptions=True,
                )

                candidate_adrs: list[ExtractedADR] = (
                    results[0] if isinstance(results[0], list) else []
                )
                candidate_constraints: list[ExtractedConstraint] = (
                    results[1] if isinstance(results[1], list) else []
                )
                candidate_risks: list[ExtractedRisk] = (
                    results[2] if isinstance(results[2], list) else []
                )
                source = seg.text_content or ""
                candidate_adrs = [
                    a.model_copy(
                        update={"final_decision": s, "context": kept_detail(a.context, source)}
                    )
                    for a in candidate_adrs
                    if (s := self._grounded(a.final_decision, source))
                ]
                candidate_constraints = [
                    c.model_copy(update={"description": s})
                    for c in candidate_constraints
                    if (s := self._grounded(c.description, source))
                ]
                candidate_risks = [
                    r.model_copy(update={"description": s})
                    for r in candidate_risks
                    if (s := self._grounded(r.description, source))
                ]
                candidate_adrs = [
                    a for a in candidate_adrs if await self._settled(source, a.final_decision)
                ]
                candidate_constraints = [
                    c for c in candidate_constraints if await self._settled(source, c.description)
                ]
                candidate_adrs, candidate_constraints, candidate_risks = one_per_statement(
                    candidate_adrs, candidate_constraints, candidate_risks
                )

                if self.sync_judge:
                    verified_adrs: list[ExtractedADR] = []
                    for adr in candidate_adrs:
                        detail = await self._judged(seg, adr.final_decision, adr.context)
                        if detail is None:
                            self.rejected_extractions_count += 1
                        else:
                            verified_adrs.append(adr.model_copy(update={"context": detail}))

                    verified_constraints: list[ExtractedConstraint] = []
                    for constraint in candidate_constraints:
                        if await self._judged(seg, constraint.description) is not None:
                            verified_constraints.append(constraint)
                        else:
                            self.rejected_extractions_count += 1

                    verified_risks: list[ExtractedRisk] = []
                    for risk in candidate_risks:
                        if await self._judged(seg, risk.description) is not None:
                            verified_risks.append(risk)
                        else:
                            self.rejected_extractions_count += 1

                    self.extracted_adrs.extend(verified_adrs)
                    self.extracted_constraints.extend(verified_constraints)
                    self.extracted_risks.extend(verified_risks)

                    if self.engine is not None:
                        async with tenant_context(self.engine, event.tenant_id) as conn:
                            await self.store.save_segment_with_extractions(
                                conn,
                                seg,
                                verified_adrs,
                                verified_constraints,
                                verified_risks,
                                connection=event.connection,
                                author=event.author,
                                access_control=event.access_control,
                            )

                    # Republish a verified extraction onto the ingest
                    # pipeline so it becomes a typed record through the
                    # usual dedup path. Best effort: the extraction is
                    # already saved, so a publish failure must not fail
                    # the segment.
                    try:
                        for adr in verified_adrs:
                            await publish_extraction_as_context_candidate(
                                self.bus,
                                tenant_id=event.tenant_id,
                                context_type="adr",
                                content=format_adr_content(adr.final_decision, adr.context, source),
                                author=event.author,
                                access_control=event.access_control,
                                connection=event.connection,
                                source_id=f"extraction-adr-{adr.id}",
                            )
                        for constraint in verified_constraints:
                            await publish_extraction_as_context_candidate(
                                self.bus,
                                tenant_id=event.tenant_id,
                                context_type="constraint",
                                content=format_constraint_content(constraint.description),
                                author=event.author,
                                access_control=event.access_control,
                                connection=event.connection,
                                source_id=f"extraction-constraint-{constraint.id}",
                            )
                        for risk in verified_risks:
                            await publish_extraction_as_context_candidate(
                                self.bus,
                                tenant_id=event.tenant_id,
                                context_type="risk",
                                content=format_risk_content(risk.description),
                                author=event.author,
                                access_control=event.access_control,
                                connection=event.connection,
                                source_id=f"extraction-risk-{risk.id}",
                            )
                    except Exception as e:
                        log.warning(
                            "transcript_worker.context_candidate_publish_failed", error=str(e)
                        )
                else:
                    # Asynchronous OpenAI Batch API Path (Default)
                    self.extracted_adrs.extend(candidate_adrs)
                    self.extracted_constraints.extend(candidate_constraints)
                    self.extracted_risks.extend(candidate_risks)

                    if self.engine is not None:
                        async with tenant_context(self.engine, event.tenant_id) as conn:
                            await self.store.save_segment_with_extractions(
                                conn,
                                seg,
                                candidate_adrs,
                                candidate_constraints,
                                candidate_risks,
                                connection=event.connection,
                                author=event.author,
                                access_control=event.access_control,
                            )

            log.info(
                "pipeline.transcript_worker.pipeline_complete",
                saved_adrs=len(self.extracted_adrs),
                saved_constraints=len(self.extracted_constraints),
                saved_risks=len(self.extracted_risks),
                sync_judge=self.sync_judge,
            )

        except Exception as e:
            log.exception(
                "pipeline.transcript_worker.failed",
                error=str(e),
            )
            # Re-raise so RedisStreamsEventBus increments retry count and handles DLQ
            raise

    async def _settled(self, source: str, statement: str) -> bool:
        if self.settled_judge is None:
            from checkcord.llm.client import LLMClient

            self.settled_judge = LLMClient.from_settings(get_settings())
        if await self.settled_judge.judge_settled(source, statement) == "SETTLED":
            return True
        increment_counter("chat_outcome_not_settled_total", tags={"platform": "meeting"})
        self.rejected_extractions_count += 1
        return False

    async def _judged(self, seg: DiscourseSegment, statement: str, detail: str = "") -> str | None:
        if detail:
            audit = await verify_extraction(
                seg, judge_payload(statement, detail), llm_client=self.llm_client
            )
            if audit.is_supported:
                return detail
        audit = await verify_extraction(seg, judge_payload(statement), llm_client=self.llm_client)
        return "" if audit.is_supported else None

    def _grounded(self, statement: str, source: str) -> str | None:
        if statement_supported(statement, source):
            return statement
        sentence = supporting_sentence(statement, source)
        if sentence is not None:
            return sentence
        increment_counter("chat_outcome_unfaithful_total", tags={"stage": "meeting"})
        self.rejected_extractions_count += 1
        return None

    async def _guarded_extract_adr(self, segment: DiscourseSegment) -> list[ExtractedADR]:
        async with self.semaphore:
            return await extract_adr(segment, llm_client=self.llm_client)  # type: ignore[no-any-return]

    async def _guarded_extract_constraints(
        self, segment: DiscourseSegment
    ) -> list[ExtractedConstraint]:
        async with self.semaphore:
            return await extract_constraints(segment, llm_client=self.llm_client)  # type: ignore[no-any-return]

    async def _guarded_extract_risks(self, segment: DiscourseSegment) -> list[ExtractedRisk]:
        async with self.semaphore:
            return await extract_risks(segment, llm_client=self.llm_client)  # type: ignore[no-any-return]

    async def _subscribe_to_topic(self, topic: str) -> None:
        """Subscription loop task."""
        try:
            await self.bus.subscribe(
                topic=topic,
                group=self.group_name,
                consumer_name=self.consumer_name,
                handler=self.process_message,
            )
        except asyncio.CancelledError:
            logger.info("transcript_worker.subscription_cancelled", topic=topic)
        except Exception as e:
            logger.exception("transcript_worker.subscription_crashed", topic=topic, error=str(e))

    async def start(self, topics: list[str]) -> None:
        """Start worker loop for given topics and block until stopped."""
        logger.info("transcript_worker.starting", topics=topics, group=self.group_name)
        tasks = [asyncio.create_task(self._subscribe_to_topic(topic)) for topic in topics]

        await self.stop_event.wait()
        logger.info("transcript_worker.shutdown_initiated")

        for t in tasks:
            t.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("transcript_worker.shutdown_complete")

    def stop(self) -> None:
        """Signal worker to stop gracefully."""
        self.stop_event.set()
