import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import ConstraintType, RiskLevel, SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.models.extractions import (
    ExtractedADR,
    ExtractedConstraint,
    ExtractedRisk,
    VerifiedExtraction,
)
from checkcord.pipeline.agents.extraction_publisher import format_adr_content
from checkcord.pipeline.agents.preprocessor import LLMSegment, SegmentationOutput
from checkcord.pipeline.agents.specialists import ADROutput, ConstraintOutput, RiskOutput
from checkcord.workers import transcript_worker as tw

H21 = "\n".join(
    [
        "[00:00:05] Tomas Brandt: Thanks for joining. Payouts timing first.",
        "[00:00:20] guest@partner.example: From our side any time before 03:00 works.",
        "[00:00:40] Aiko Sato: Batching at night keeps bank fees down.",
        "[00:01:00] Tomas Brandt: Decision then: payouts run daily at 02:00 UTC.",
        "[00:01:20] Tomas Brandt: Action item for Aiko: update the scheduler config by Friday.",
        "[00:01:40] Rosa Imani: Unrelated, but has anyone tried the new ramen place?",
    ]
)
DECISION = "ledger.post_entry is the only function allowed to write to the ledger table"
E20 = "\n".join(
    [
        "[00:00:05] Tomas Brandt: OK, the ledger. We have three places writing rows directly.",
        "[00:00:20] Aiko Sato: Refunds and the reconciliation job both insert by hand.",
        "[00:00:40] Rosa Imani: That is how the duplicate entries happened last month.",
        f"[00:01:00] Tomas Brandt: Right. So we decided that {DECISION}.",
        "[00:01:20] Aiko Sato: Agreed. I will move the refund path over.",
    ]
)


def _event(text: str) -> CanonicalEvent:
    return CanonicalEvent(
        tenant_id=uuid4(),
        source_platform=SourcePlatform.ZOOM,
        source_type=SourceType.MEETING_TRANSCRIPT,
        source_id="zoom_1",
        happened_at=datetime.now(UTC),
        author=AuthorInfo(source_id="host"),
        content_raw=text,
        content_clean=text,
        access_control=AccessControl(is_public=True),
    )


def _worker(text: str, outputs: dict[Any, list[Any]], verdict: Any = None) -> tuple[Any, AsyncMock]:
    llm = MagicMock()
    llm.judge_settled = AsyncMock(return_value="SETTLED")

    def structured(schema: Any) -> Any:
        call = AsyncMock()
        if schema == SegmentationOutput:
            call.ainvoke.return_value = SegmentationOutput(
                segments=[LLMSegment(start_time=0.0, end_time=60.0, text_content=text)]
            )
        elif schema == VerifiedExtraction:
            if verdict is None:
                call.ainvoke.return_value = VerifiedExtraction(is_supported=True)
            else:
                call.ainvoke.side_effect = verdict
        else:
            call.ainvoke.return_value = schema(items=outputs.get(schema, []))
        return call

    llm.with_structured_output.side_effect = structured
    bus = MagicMock()
    bus.publish = AsyncMock()
    return tw.TranscriptExtractionWorker(bus=bus, llm_client=llm, sync_judge=True), bus.publish


def _published(publish: AsyncMock) -> list[str]:
    return [c.args[1].content_clean for c in publish.await_args_list]


def test_supporting_sentence_replaces_a_paraphrase() -> None:
    assert (
        tw.supporting_sentence("Payouts will run every day at 2 AM UTC", H21)
        == "Decision then: payouts run daily at 02:00 UTC."
    )
    assert tw.supporting_sentence("Refunds are paused for the holidays", H21) is None


def test_record_text_is_the_statement_with_kept_context() -> None:
    assert format_adr_content(DECISION, "Duplicate entries happened last month", E20) == (
        f"{DECISION}\n- Duplicate entries happened last month"
    )
    assert format_adr_content(DECISION, "Auditors demanded a single writer", E20) == DECISION


@pytest.mark.asyncio
async def test_h21_paraphrase_becomes_the_meeting_sentence() -> None:
    adr = ExtractedADR(
        decision_title="Payout schedule",
        context="Bank fees",
        final_decision="Payouts will run every day at 2am UTC",
    )
    worker, publish = _worker(H21, {ADROutput: [adr]})
    await worker.process_message(_event(H21))
    assert [a.final_decision for a in worker.extracted_adrs] == [
        "Decision then: payouts run daily at 02:00 UTC."
    ]
    assert _published(publish) == ["Decision then: payouts run daily at 02:00 UTC.\n- Bank fees"]


@pytest.mark.asyncio
async def test_e20_one_record_per_statement() -> None:
    adr = ExtractedADR(
        decision_title="Ledger write path",
        context="the duplicate entries happened last month",
        final_decision=DECISION,
    )
    constraint = ExtractedConstraint(
        constraint_type=ConstraintType.TECHNICAL,
        description=f"Only {DECISION}",
        source_quote=DECISION,
    )
    risk = ExtractedRisk(
        risk_level=RiskLevel.MED,
        description="Refunds and the reconciliation job both insert by hand",
    )
    worker, publish = _worker(
        E20, {ADROutput: [adr], ConstraintOutput: [constraint], RiskOutput: [risk]}
    )
    await worker.process_message(_event(E20))
    assert len(worker.extracted_adrs) == 1
    assert worker.extracted_constraints == []
    assert _published(publish) == [
        f"{DECISION}\n- the duplicate entries happened last month",
        "Refunds and the reconciliation job both insert by hand",
    ]


def test_overlap_is_zero_without_content_words() -> None:
    assert tw.overlap("ok", DECISION) == 0.0
    assert tw.overlap("payouts run hourly", "payouts run daily at 02:00 UTC") == 0.5


def _judge(seen: list[dict[str, Any]], supported: Any) -> Any:
    async def verdict(messages: Any, *args: Any, **kwargs: Any) -> VerifiedExtraction:
        payload = json.loads(messages[1][1].split("Candidate Extraction Payload:\n", 1)[1])
        seen.append(payload)
        return VerifiedExtraction(is_supported=supported(payload))

    return verdict


H21_ADR = ExtractedADR(
    decision_title="Payout schedule",
    context="Bank fees",
    alternatives_considered=["hourly"],
    primary_driver="bank fees",
    final_decision="Payouts will run every day at 2am UTC",
)
H21_SENTENCE = "Decision then: payouts run daily at 02:00 UTC."


@pytest.mark.asyncio
async def test_detail_rejected_statement_kept_without_it() -> None:
    seen: list[dict[str, Any]] = []
    worker, publish = _worker(
        H21, {ADROutput: [H21_ADR]}, _judge(seen, lambda p: "detail" not in p)
    )
    await worker.process_message(_event(H21))
    assert seen == [{"statement": H21_SENTENCE, "detail": "Bank fees"}, {"statement": H21_SENTENCE}]
    assert _published(publish) == [H21_SENTENCE]
    assert worker.rejected_extractions_count == 0


@pytest.mark.asyncio
async def test_rejected_twice_counts_once() -> None:
    seen: list[dict[str, Any]] = []
    worker, publish = _worker(H21, {ADROutput: [H21_ADR]}, _judge(seen, lambda p: False))
    await worker.process_message(_event(H21))
    assert len(seen) == 2
    assert _published(publish) == []
    assert worker.extracted_adrs == []
    assert worker.rejected_extractions_count == 1


@pytest.mark.asyncio
async def test_judge_sees_only_what_is_stored() -> None:
    seen: list[dict[str, Any]] = []
    adr = H21_ADR.model_copy(update={"context": "Auditors demanded a single writer"})
    worker, _ = _worker(H21, {ADROutput: [adr]}, _judge(seen, lambda p: True))
    await worker.process_message(_event(H21))
    assert seen == [{"statement": H21_SENTENCE}]
    assert [a.context for a in worker.extracted_adrs] == [""]
