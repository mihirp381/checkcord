from uuid import UUID, uuid4

from pydantic import Field

from checkcord.models.common import FrozenModel
from checkcord.models.enums import AuditStatus, ConstraintType, RiskLevel


class DiscourseSegment(FrozenModel):
    """One topic of a meeting transcript."""

    segment_id: UUID = Field(default_factory=uuid4)
    parent_event_id: UUID
    tenant_id: UUID
    start_time: float = 0.0
    end_time: float = 0.0
    topics: list[str] = Field(default_factory=list)
    speakers: list[str] = Field(default_factory=list)
    text_content: str
    confidence_score: float = 1.0


class ExtractedADRContent(FrozenModel):
    """LLM-facing ADR extraction schema.

    Has no `id` field: asked to produce one, a model repeats the same
    placeholder UUID across unrelated meetings and causes primary-key
    collisions. `ExtractedADR` adds the id after parsing.
    """

    decision_title: str
    context: str
    alternatives_considered: list[str] = Field(default_factory=list)
    final_decision: str
    primary_driver: str | None = None
    audit_status: AuditStatus = AuditStatus.PENDING_AUDIT


class ExtractedADR(ExtractedADRContent):
    """Architecture Decision Record extracted from a discourse segment."""

    id: UUID = Field(default_factory=uuid4)


class ExtractedConstraintContent(FrozenModel):
    """LLM-facing constraint extraction schema — see ExtractedADRContent for why `id` is omitted."""  # noqa: E501

    constraint_type: ConstraintType
    description: str
    source_quote: str
    audit_status: AuditStatus = AuditStatus.PENDING_AUDIT


class ExtractedConstraint(ExtractedConstraintContent):
    """Business rule or constraint extracted from a discourse segment."""

    id: UUID = Field(default_factory=uuid4)


class ExtractedRiskContent(FrozenModel):
    """LLM-facing risk extraction schema — see ExtractedADRContent for why `id` is omitted."""

    risk_level: RiskLevel
    description: str
    potential_mitigation: str | None = None
    deferred_action_items: list[str] = Field(default_factory=list)
    audit_status: AuditStatus = AuditStatus.PENDING_AUDIT


class ExtractedRisk(ExtractedRiskContent):
    """Risk or customer signal extracted from a discourse segment."""

    id: UUID = Field(default_factory=uuid4)


class VerifiedExtraction(FrozenModel):
    """The verifier's answer for one extraction."""

    is_supported: bool
    hallucinated_elements: list[str] = Field(default_factory=list)
    rejection_reason: str | None = None
