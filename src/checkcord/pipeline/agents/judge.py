# ruff: noqa: E501
"""The verifier: checks a meeting extraction against the transcript text."""

import json
from typing import Any

import structlog

from checkcord.core.config import get_settings
from checkcord.core.llm_retry import ainvoke_with_retry
from checkcord.llm.factory import build_chat_model
from checkcord.llm.outcomes import _detail_kept
from checkcord.models.extractions import DiscourseSegment, VerifiedExtraction

logger = structlog.get_logger(__name__)


def kept_detail(context: str | None, source: str) -> str:
    text = (context or "").strip()
    return text if text and _detail_kept(text, source) else ""


def judge_payload(statement: str, detail: str = "") -> dict[str, str]:
    return {"statement": statement, "detail": detail} if detail else {"statement": statement}


def stored_payload(entity_type: str, payload: dict[str, Any], source: str) -> dict[str, str]:
    if entity_type == "adr":
        return judge_payload(payload["final_decision"], kept_detail(payload.get("context"), source))
    return judge_payload(payload["description"])


JUDGE_SYSTEM_PROMPT = """You are a strict Auditor and Fact-Checker.
Your task is to evaluate a JSON extraction against the verbatim text content of a meeting transcript segment.

Rules:
1. Verify if EVERY claim, decision, constraint, quote, or risk in the JSON payload is explicitly supported by the source text.
2. Be conservative. If the extraction makes ungrounded assumptions, introduces external information, or misinterprets the text, mark `is_supported=false`.
3. If `is_supported=false`, populate `hallucinated_elements` with the specific unsupported claims and state the `rejection_reason`.
4. If the text fully supports the extraction, mark `is_supported=true`.
"""


async def verify_extraction(
    segment: DiscourseSegment,
    extracted_data: dict[str, Any],
    llm_client: Any | None = None,
) -> VerifiedExtraction:
    """Audits a candidate extraction dictionary against a DiscourseSegment using gpt-4o.

    Returns a VerifiedExtraction model indicating whether the claim is grounded.
    """
    log = logger.bind(
        tenant_id=str(segment.tenant_id),
        segment_id=str(segment.segment_id),
        agent="JudgeAgent",
    )

    if not segment.text_content or not segment.text_content.strip() or not extracted_data:
        return VerifiedExtraction(
            is_supported=False,
            rejection_reason="Empty segment text or extraction payload.",
        )

    try:
        if llm_client is None:
            llm_client = build_chat_model(
                get_settings().meeting_extraction_llm_model, purpose="meeting_judge"
            )

        structured_llm = llm_client.with_structured_output(VerifiedExtraction)

        payload_str = json.dumps(extracted_data, default=str)
        user_message = (
            f"Verbatim Transcript Segment:\n{segment.text_content}\n\n"
            f"Candidate Extraction Payload:\n{payload_str}"
        )

        messages = [
            ("system", JUDGE_SYSTEM_PROMPT),
            ("user", user_message),
        ]

        result: VerifiedExtraction = await ainvoke_with_retry(
            structured_llm, messages, agent="JudgeAgent"
        )

        if result:
            log.info(
                "pipeline.judge.audit_complete",
                is_supported=result.is_supported,
                rejection_reason=result.rejection_reason,
            )
            return result
    except Exception as e:
        log.warning("pipeline.judge.audit_error", error=str(e))
        return VerifiedExtraction(
            is_supported=False,
            rejection_reason=f"LLM verification exception: {str(e)}",
        )

    return VerifiedExtraction(
        is_supported=False,
        rejection_reason="Empty response from LLM verification.",
    )
