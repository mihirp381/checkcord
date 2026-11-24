# ruff: noqa: E501
"""Extractors for decisions, constraints and risks in meeting segments."""

from typing import Any, cast

import structlog
from pydantic import BaseModel, Field

from checkcord.core.config import get_settings
from checkcord.core.llm_retry import ainvoke_with_retry
from checkcord.core.rate_limiter import global_rate_limiter
from checkcord.llm.factory import build_chat_model
from checkcord.models.cicd import ExtractedCIFailure, ExtractedCIFailureContent
from checkcord.models.extractions import (
    DiscourseSegment,
    ExtractedADR,
    ExtractedADRContent,
    ExtractedConstraint,
    ExtractedConstraintContent,
    ExtractedRisk,
    ExtractedRiskContent,
)

logger = structlog.get_logger(__name__)


class ADROutput(BaseModel):
    """Items have no `id`; extract_adr() assigns ids after parsing."""

    items: list[ExtractedADRContent] = Field(default_factory=list)


class ConstraintOutput(BaseModel):
    items: list[ExtractedConstraintContent] = Field(default_factory=list)


class RiskOutput(BaseModel):
    items: list[ExtractedRiskContent] = Field(default_factory=list)


class CIFailureOutput(BaseModel):
    items: list[ExtractedCIFailureContent] = Field(default_factory=list)


ADR_SYSTEM_PROMPT = """You are a conservative Staff Software Engineer auditing a meeting transcript.
Extract ONLY explicit, finalized technical choices or architectural decisions discussed (e.g. database selections, design patterns, infrastructure trade-offs).
CRITICAL RULES:
1. Extract ONLY facts explicitly stated in the verbatim text.
2. DO NOT extrapolate, assume, or infer unmentioned alternatives, primary drivers, or contexts.
3. If primary_driver or alternatives are not explicitly mentioned in the text, leave them NULL or empty.
4. Ignore routine code chatter or vague, undecided proposals.
"""

CONSTRAINT_SYSTEM_PROMPT = """You are a conservative Compliance & Product Domain Expert.
Extract strict business rules, regulatory requirements (GDPR, HIPAA, SOC2), budget limits, or hard operational boundaries discussed.
CRITICAL RULES:
1. Every constraint MUST be supported by an EXACT, verbatim source_quote from the text.
2. DO NOT extrapolate or rephrase the source quote.
3. Only extract constraints explicitly stated as rules or hard boundaries.
"""

RISK_SYSTEM_PROMPT = """You are a conservative Technical Program Manager.
Extract explicitly stated customer pain points, explicit tech debt, system vulnerabilities, or unresolved action items deferred to later.
CRITICAL RULES:
1. Extract ONLY risks explicitly stated in the text.
2. DO NOT speculate on unmentioned potential mitigations or risk levels.
3. If potential_mitigation is not stated in the text, leave it NULL.
"""

CI_FAILURE_SYSTEM_PROMPT = """You are a precise CI/CD failure analyst extracting structured signal from a CI failure log or annotation excerpt.
CRITICAL RULES:
1. Extract ONLY what is explicitly present in the text -- do not guess a file path, test name, or line number that isn't stated.
2. If a specific failing test/step name isn't identifiable, leave failing_test_name NULL. Same for file_path and line_number.
3. error_message must be the most relevant verbatim error/assertion/exception text, not a paraphrase or summary.
4. summary is one concise sentence describing what failed and why, written for a reader who will never see the raw log.
5. If the text describes multiple independent failures, return one item per failure; if it describes one failure, return exactly one item.
"""


async def extract_adr(
    segment: DiscourseSegment, llm_client: Any | None = None
) -> list[ExtractedADR]:
    """Isolates and extracts Architectural Decision Records (ADRs) from a DiscourseSegment."""
    log = logger.bind(
        tenant_id=str(segment.tenant_id),
        parent_event_id=str(segment.parent_event_id),
        segment_id=str(segment.segment_id),
        agent="ADRAgent",
    )

    if not segment.text_content or not segment.text_content.strip():
        return []

    try:
        if llm_client is None:
            llm_client = build_chat_model(
                get_settings().meeting_extraction_llm_model, purpose="meeting_adr"
            )

        structured_llm = llm_client.with_structured_output(ADROutput)
        messages = [
            ("system", ADR_SYSTEM_PROMPT),
            ("user", f"Discourse Segment Content:\n{segment.text_content}"),
        ]

        res = cast(
            ADROutput,
            await ainvoke_with_retry(
                structured_llm,
                messages,
                agent="ADRAgent",
                before_attempt=lambda: global_rate_limiter.acquire(1000),
            ),
        )

        if res and res.items:
            adrs = [ExtractedADR(**item.model_dump()) for item in res.items]
            log.info("pipeline.specialist.adr_success", count=len(adrs))
            return adrs
    except Exception as e:
        log.warning("pipeline.specialist.adr_error", error=str(e))

    return []


async def extract_constraints(
    segment: DiscourseSegment, llm_client: Any | None = None
) -> list[ExtractedConstraint]:
    """Isolates and extracts business/technical constraints from a DiscourseSegment."""
    log = logger.bind(
        tenant_id=str(segment.tenant_id),
        parent_event_id=str(segment.parent_event_id),
        segment_id=str(segment.segment_id),
        agent="ConstraintAgent",
    )

    if not segment.text_content or not segment.text_content.strip():
        return []

    try:
        if llm_client is None:
            llm_client = build_chat_model(
                get_settings().meeting_extraction_llm_model, purpose="meeting_constraints"
            )

        structured_llm = llm_client.with_structured_output(ConstraintOutput)
        messages = [
            ("system", CONSTRAINT_SYSTEM_PROMPT),
            ("user", f"Discourse Segment Content:\n{segment.text_content}"),
        ]

        res = cast(
            ConstraintOutput,
            await ainvoke_with_retry(
                structured_llm,
                messages,
                agent="ConstraintAgent",
                before_attempt=lambda: global_rate_limiter.acquire(1000),
            ),
        )
        if res and res.items:
            constraints = [ExtractedConstraint(**item.model_dump()) for item in res.items]
            log.info("pipeline.specialist.constraint_success", count=len(constraints))
            return constraints
    except Exception as e:
        log.warning("pipeline.specialist.constraint_error", error=str(e))

    return []


async def extract_risks(
    segment: DiscourseSegment, llm_client: Any | None = None
) -> list[ExtractedRisk]:
    """Isolates and extracts risks and customer signals from a DiscourseSegment."""
    log = logger.bind(
        tenant_id=str(segment.tenant_id),
        parent_event_id=str(segment.parent_event_id),
        segment_id=str(segment.segment_id),
        agent="RiskAgent",
    )

    if not segment.text_content or not segment.text_content.strip():
        return []

    try:
        if llm_client is None:
            llm_client = build_chat_model(
                get_settings().meeting_extraction_llm_model, purpose="meeting_risks"
            )

        structured_llm = llm_client.with_structured_output(RiskOutput)
        messages = [
            ("system", RISK_SYSTEM_PROMPT),
            ("user", f"Discourse Segment Content:\n{segment.text_content}"),
        ]

        res = cast(
            RiskOutput,
            await ainvoke_with_retry(
                structured_llm,
                messages,
                agent="RiskAgent",
                before_attempt=lambda: global_rate_limiter.acquire(1000),
            ),
        )
        if res and res.items:
            risks = [ExtractedRisk(**item.model_dump()) for item in res.items]
            log.info("pipeline.specialist.risk_success", count=len(risks))
            return risks
    except Exception as e:
        log.warning("pipeline.specialist.risk_error", error=str(e))

    return []


async def extract_ci_failure(
    segment: DiscourseSegment, llm_client: Any | None = None
) -> list[ExtractedCIFailure]:
    """Isolates structured failure signal (failing test, file, error message)
    from CI failure content (Checks-API annotations or a bounded job-log
    excerpt, wrapped into a DiscourseSegment by the caller).
    """
    log = logger.bind(
        tenant_id=str(segment.tenant_id),
        parent_event_id=str(segment.parent_event_id),
        segment_id=str(segment.segment_id),
        agent="CIFailureAgent",
    )

    if not segment.text_content or not segment.text_content.strip():
        return []

    try:
        if llm_client is None:
            llm_client = build_chat_model(
                get_settings().ci_failure_extraction_llm_model, purpose="ci_failure"
            )

        structured_llm = llm_client.with_structured_output(CIFailureOutput)
        messages = [
            ("system", CI_FAILURE_SYSTEM_PROMPT),
            ("user", f"CI Failure Content:\n{segment.text_content}"),
        ]

        res = cast(
            CIFailureOutput,
            await ainvoke_with_retry(
                structured_llm,
                messages,
                agent="CIFailureAgent",
                before_attempt=lambda: global_rate_limiter.acquire(1000),
            ),
        )

        if res and res.items:
            failures = [ExtractedCIFailure(**item.model_dump()) for item in res.items]
            log.info("pipeline.specialist.ci_failure_success", count=len(failures))
            return failures
    except Exception as e:
        log.warning("pipeline.specialist.ci_failure_error", error=str(e))

    return []
