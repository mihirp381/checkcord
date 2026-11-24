from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from checkcord.models.cicd import ExtractedCIFailureContent
from checkcord.models.enums import ConstraintType, RiskLevel
from checkcord.models.extractions import (
    DiscourseSegment,
    ExtractedADRContent,
    ExtractedConstraintContent,
    ExtractedRiskContent,
)
from checkcord.pipeline.agents.specialists import (
    ADROutput,
    CIFailureOutput,
    ConstraintOutput,
    RiskOutput,
    extract_adr,
    extract_ci_failure,
    extract_constraints,
    extract_risks,
)


def make_dummy_segment(text: str = "Sample segment text") -> DiscourseSegment:
    return DiscourseSegment(
        parent_event_id=uuid4(),
        tenant_id=uuid4(),
        text_content=text,
        topics=["architecture"],
    )


@pytest.mark.asyncio
async def test_extract_adr_empty_text() -> None:
    segment = make_dummy_segment("")
    result = await extract_adr(segment)
    assert result == []


@pytest.mark.asyncio
async def test_extract_adr_success() -> None:
    segment = make_dummy_segment("Alice: We decided to use Postgres for strict RLS.")

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.return_value = ADROutput(
        items=[
            ExtractedADRContent(
                decision_title="Use Postgres",
                context="Multi-tenant isolation requirement",
                alternatives_considered=["MongoDB"],
                final_decision="Postgres RLS",
                primary_driver="Alice",
            )
        ]
    )

    mock_llm_client = MagicMock()
    mock_llm_client.with_structured_output.return_value = mock_structured_llm

    adrs = await extract_adr(segment, llm_client=mock_llm_client)
    assert len(adrs) == 1
    assert adrs[0].decision_title == "Use Postgres"
    assert adrs[0].id is not None


@pytest.mark.asyncio
async def test_extract_adr_retries_once_then_succeeds() -> None:
    """A first-attempt validation failure is not fatal: extract_adr retries
    and returns the recovered result, not [].
    """
    segment = make_dummy_segment("Alice: We decided to use Postgres for strict RLS.")

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.side_effect = [
        ValueError("schema validation failed: invalid output"),
        ADROutput(
            items=[
                ExtractedADRContent(
                    decision_title="Use Postgres",
                    context="Multi-tenant isolation requirement",
                    final_decision="Postgres RLS",
                )
            ]
        ),
    ]

    mock_llm_client = MagicMock()
    mock_llm_client.with_structured_output.return_value = mock_structured_llm

    adrs = await extract_adr(segment, llm_client=mock_llm_client)
    assert len(adrs) == 1
    assert adrs[0].decision_title == "Use Postgres"
    assert mock_structured_llm.ainvoke.call_count == 2


@pytest.mark.asyncio
async def test_extract_adr_llm_error() -> None:
    segment = make_dummy_segment("Some text")

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.side_effect = RuntimeError("OpenAI 500 error")

    mock_llm_client = MagicMock()
    mock_llm_client.with_structured_output.return_value = mock_structured_llm

    adrs = await extract_adr(segment, llm_client=mock_llm_client)
    assert adrs == []


@pytest.mark.asyncio
async def test_extract_constraints_success() -> None:
    segment = make_dummy_segment("Bob: GDPR states all user data must be deleted upon request.")

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.return_value = ConstraintOutput(
        items=[
            ExtractedConstraintContent(
                constraint_type=ConstraintType.REGULATORY,
                description="GDPR user deletion mandate",
                source_quote="GDPR states all user data must be deleted upon request.",
            )
        ]
    )

    mock_llm_client = MagicMock()
    mock_llm_client.with_structured_output.return_value = mock_structured_llm

    constraints = await extract_constraints(segment, llm_client=mock_llm_client)
    assert len(constraints) == 1
    assert constraints[0].constraint_type == ConstraintType.REGULATORY


@pytest.mark.asyncio
async def test_extract_constraints_retries_once_then_succeeds() -> None:
    """Same retry coverage as extract_adr, for extract_constraints."""
    segment = make_dummy_segment("Bob: GDPR states all user data must be deleted upon request.")

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.side_effect = [
        ValueError("schema validation failed: invalid output"),
        ConstraintOutput(
            items=[
                ExtractedConstraintContent(
                    constraint_type=ConstraintType.REGULATORY,
                    description="GDPR user deletion mandate",
                    source_quote="GDPR states all user data must be deleted upon request.",
                )
            ]
        ),
    ]

    mock_llm_client = MagicMock()
    mock_llm_client.with_structured_output.return_value = mock_structured_llm

    constraints = await extract_constraints(segment, llm_client=mock_llm_client)
    assert len(constraints) == 1
    assert constraints[0].constraint_type == ConstraintType.REGULATORY
    assert mock_structured_llm.ainvoke.call_count == 2


@pytest.mark.asyncio
async def test_extract_constraints_llm_error() -> None:
    segment = make_dummy_segment("Some text")

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.side_effect = RuntimeError("Parse error")

    mock_llm_client = MagicMock()
    mock_llm_client.with_structured_output.return_value = mock_structured_llm

    constraints = await extract_constraints(segment, llm_client=mock_llm_client)
    assert constraints == []


@pytest.mark.asyncio
async def test_extract_risks_success() -> None:
    segment = make_dummy_segment("Charlie: Unhandled API limits could crash the consumer.")

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.return_value = RiskOutput(
        items=[
            ExtractedRiskContent(
                risk_level=RiskLevel.HIGH,
                description="LLM rate limit crash risk",
                potential_mitigation="Add Semaphore and retry backoff",
            )
        ]
    )

    mock_llm_client = MagicMock()
    mock_llm_client.with_structured_output.return_value = mock_structured_llm

    risks = await extract_risks(segment, llm_client=mock_llm_client)
    assert len(risks) == 1
    assert risks[0].risk_level == RiskLevel.HIGH


@pytest.mark.asyncio
async def test_extract_risks_retries_once_then_succeeds() -> None:
    """Same retry coverage as extract_adr, for extract_risks."""
    segment = make_dummy_segment("Charlie: Unhandled API limits could crash the consumer.")

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.side_effect = [
        ValueError("schema validation failed: invalid output"),
        RiskOutput(
            items=[
                ExtractedRiskContent(
                    risk_level=RiskLevel.HIGH,
                    description="LLM rate limit crash risk",
                    potential_mitigation="Add Semaphore and retry backoff",
                )
            ]
        ),
    ]

    mock_llm_client = MagicMock()
    mock_llm_client.with_structured_output.return_value = mock_structured_llm

    risks = await extract_risks(segment, llm_client=mock_llm_client)
    assert len(risks) == 1
    assert risks[0].risk_level == RiskLevel.HIGH
    assert mock_structured_llm.ainvoke.call_count == 2


@pytest.mark.asyncio
async def test_extract_risks_llm_error() -> None:
    segment = make_dummy_segment("Some text")

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.side_effect = RuntimeError("Network error")

    mock_llm_client = MagicMock()
    mock_llm_client.with_structured_output.return_value = mock_structured_llm

    risks = await extract_risks(segment, llm_client=mock_llm_client)
    assert risks == []


# ---------------------------------------------------------------------------
# `id` must not appear in the LLM-facing structured-output schema, and each
# extraction gets its own Python-assigned id even when the LLM returns
# identical content twice.
# ---------------------------------------------------------------------------


def test_adr_output_schema_has_no_id_field() -> None:
    """The JSON schema handed to `with_structured_output` must not contain `id`.

    Before the fix, ADROutput.items was list[ExtractedADR], which does have an
    `id: UUID` field — so the LLM saw `id` as something it had to produce a value
    for. Same check for ConstraintOutput/RiskOutput below.
    """
    schema = ADROutput.model_json_schema()
    adr_content_schema = schema["$defs"]["ExtractedADRContent"]
    assert "id" not in adr_content_schema["properties"]


def test_constraint_output_schema_has_no_id_field() -> None:
    schema = ConstraintOutput.model_json_schema()
    constraint_content_schema = schema["$defs"]["ExtractedConstraintContent"]
    assert "id" not in constraint_content_schema["properties"]


def test_risk_output_schema_has_no_id_field() -> None:
    schema = RiskOutput.model_json_schema()
    risk_content_schema = schema["$defs"]["ExtractedRiskContent"]
    assert "id" not in risk_content_schema["properties"]


@pytest.mark.asyncio
async def test_extract_adr_exhausts_retries_then_falls_through_to_empty() -> None:
    """A systematically wrong schema does not retry forever: it exhausts the
    cap (3 total attempts) and then returns the empty list.
    """
    segment = make_dummy_segment("Some text")

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.side_effect = RuntimeError("persistently invalid schema")

    mock_llm_client = MagicMock()
    mock_llm_client.with_structured_output.return_value = mock_structured_llm

    adrs = await extract_adr(segment, llm_client=mock_llm_client)
    assert adrs == []
    assert mock_structured_llm.ainvoke.call_count == 3  # initial attempt + 2 retries


@pytest.mark.asyncio
async def test_extract_adr_assigns_distinct_ids_for_identical_content() -> None:
    """Two LLM calls returning identical content must not produce colliding ids.

    With `id` exposed to the LLM, a model can return the same placeholder UUID
    twice. The id is stripped from the LLM-facing schema and assigned by Python.
    """
    segment = make_dummy_segment("Alice: We decided to use Postgres for strict RLS.")
    identical_content = ExtractedADRContent(
        decision_title="Use Postgres",
        context="Multi-tenant isolation requirement",
        final_decision="Postgres RLS",
    )

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.return_value = ADROutput(items=[identical_content])
    mock_llm_client = MagicMock()
    mock_llm_client.with_structured_output.return_value = mock_structured_llm

    first_call = await extract_adr(segment, llm_client=mock_llm_client)
    second_call = await extract_adr(segment, llm_client=mock_llm_client)

    assert first_call[0].id != second_call[0].id
    # Sanity: content itself is genuinely identical, isolating id as the only variable.
    assert first_call[0].decision_title == second_call[0].decision_title


# ---------------------------------------------------------------------------
# extract_ci_failure follows the same with_structured_output +
# ainvoke_with_retry pattern as the extractors above.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_ci_failure_empty_text() -> None:
    segment = make_dummy_segment("")
    result = await extract_ci_failure(segment)
    assert result == []


@pytest.mark.asyncio
async def test_extract_ci_failure_success() -> None:
    segment = make_dummy_segment(
        "tests/test_payment_webhook.py:44: AssertionError: expected 200, got 500"
    )

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.return_value = CIFailureOutput(
        items=[
            ExtractedCIFailureContent(
                failing_test_name="test_payment_webhook",
                file_path="tests/test_payment_webhook.py",
                line_number=44,
                error_message="AssertionError: expected 200, got 500",
                summary="test_payment_webhook failed: expected HTTP 200 but got 500",
            )
        ]
    )

    mock_llm_client = MagicMock()
    mock_llm_client.with_structured_output.return_value = mock_structured_llm

    failures = await extract_ci_failure(segment, llm_client=mock_llm_client)
    assert len(failures) == 1
    assert failures[0].failing_test_name == "test_payment_webhook"
    assert failures[0].file_path == "tests/test_payment_webhook.py"
    assert failures[0].line_number == 44
    assert "500" in failures[0].error_message
    mock_llm_client.with_structured_output.assert_called_once_with(CIFailureOutput)


@pytest.mark.asyncio
async def test_extract_ci_failure_llm_error_returns_empty_list() -> None:
    segment = make_dummy_segment("some failure log content")

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.side_effect = RuntimeError("persistently invalid schema")

    mock_llm_client = MagicMock()
    mock_llm_client.with_structured_output.return_value = mock_structured_llm

    failures = await extract_ci_failure(segment, llm_client=mock_llm_client)
    assert failures == []
    assert mock_structured_llm.ainvoke.call_count == 3  # initial attempt + 2 retries


@pytest.mark.asyncio
async def test_extract_ci_failure_assigns_distinct_ids_for_identical_content() -> None:
    """Ids are assigned by Python, as for ADR/Constraint/Risk."""
    segment = make_dummy_segment("duplicate failure text")
    identical_content = ExtractedCIFailureContent(
        failing_test_name="test_x",
        file_path="src/x.py",
        line_number=10,
        error_message="boom",
        summary="test_x failed",
    )

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.return_value = CIFailureOutput(items=[identical_content])
    mock_llm_client = MagicMock()
    mock_llm_client.with_structured_output.return_value = mock_structured_llm

    first_call = await extract_ci_failure(segment, llm_client=mock_llm_client)
    second_call = await extract_ci_failure(segment, llm_client=mock_llm_client)

    assert first_call[0].id != second_call[0].id
    assert first_call[0].summary == second_call[0].summary


@pytest.mark.asyncio
async def test_extract_ci_failure_no_items_returns_empty_list() -> None:
    segment = make_dummy_segment("no clear failure signal here")

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.return_value = CIFailureOutput(items=[])
    mock_llm_client = MagicMock()
    mock_llm_client.with_structured_output.return_value = mock_structured_llm

    failures = await extract_ci_failure(segment, llm_client=mock_llm_client)
    assert failures == []
