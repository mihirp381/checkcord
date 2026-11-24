from uuid import uuid4

import pytest
from pydantic import ValidationError

from checkcord.models.enums import ConstraintType, RiskLevel
from checkcord.models.extractions import (
    DiscourseSegment,
    ExtractedADR,
    ExtractedConstraint,
    ExtractedRisk,
    VerifiedExtraction,
)


def test_discourse_segment_valid() -> None:
    parent_id = uuid4()
    tenant_id = uuid4()
    segment = DiscourseSegment(
        parent_event_id=parent_id,
        tenant_id=tenant_id,
        text_content="This is a test transcript chunk.",
        topics=["architecture", "database"],
        speakers=["Alice", "Bob"],
    )
    assert segment.parent_event_id == parent_id
    assert segment.tenant_id == tenant_id
    assert segment.confidence_score == 1.0
    assert len(segment.topics) == 2


def test_discourse_segment_missing_required() -> None:
    with pytest.raises(ValidationError):
        # Missing tenant_id and parent_event_id
        DiscourseSegment(text_content="No IDs provided")


def test_discourse_segment_frozen() -> None:
    segment = DiscourseSegment(
        parent_event_id=uuid4(),
        tenant_id=uuid4(),
        text_content="Immutable text",
    )
    with pytest.raises(ValidationError):
        segment.text_content = "Mutated"


def test_extracted_adr() -> None:
    adr = ExtractedADR(
        decision_title="Use Postgres RLS",
        context="Multi-tenant isolation requirement",
        alternatives_considered=["Application level filtering", "Database per tenant"],
        final_decision="Postgres Row-Level Security",
        primary_driver="Bob",
    )
    assert adr.decision_title == "Use Postgres RLS"
    assert len(adr.alternatives_considered) == 2


def test_extracted_constraint() -> None:
    constraint = ExtractedConstraint(
        constraint_type=ConstraintType.REGULATORY,
        description="GDPR compliance for user data deletion",
        source_quote="We must ensure all user data can be deleted upon request under GDPR.",
    )
    assert constraint.constraint_type == ConstraintType.REGULATORY


def test_extracted_risk() -> None:
    risk = ExtractedRisk(
        risk_level=RiskLevel.HIGH,
        description="Potential LLM API rate limiting during high traffic",
        potential_mitigation="Implement fallback deterministic chunker",
        deferred_action_items=["Benchmark fallback performance"],
    )
    assert risk.risk_level == RiskLevel.HIGH


def test_verified_extraction() -> None:
    verified = VerifiedExtraction(
        is_supported=True,
    )
    assert verified.is_supported is True
    assert verified.hallucinated_elements == []
