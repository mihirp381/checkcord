from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from checkcord.models.extractions import DiscourseSegment, VerifiedExtraction
from checkcord.pipeline.agents.judge import verify_extraction


def make_dummy_segment(text: str = "Alice: We chose Postgres.") -> DiscourseSegment:
    return DiscourseSegment(
        parent_event_id=uuid4(),
        tenant_id=uuid4(),
        text_content=text,
        topics=["db"],
    )


@pytest.mark.asyncio
async def test_verify_extraction_empty() -> None:
    seg = make_dummy_segment("")
    res = await verify_extraction(seg, {"decision": "Postgres"})
    assert res.is_supported is False
    assert "Empty" in (res.rejection_reason or "")


@pytest.mark.asyncio
async def test_verify_extraction_supported() -> None:
    seg = make_dummy_segment("Alice: We chose Postgres for database isolation.")
    payload = {"decision_title": "Use Postgres"}

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.return_value = VerifiedExtraction(
        is_supported=True,
    )

    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = mock_structured_llm

    res = await verify_extraction(seg, payload, llm_client=mock_llm)
    assert res.is_supported is True
    assert res.hallucinated_elements == []


@pytest.mark.asyncio
async def test_verify_extraction_unsupported() -> None:
    seg = make_dummy_segment("Alice: We might look into MongoDB later.")
    payload = {"decision_title": "We officially migrated to MongoDB."}

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.return_value = VerifiedExtraction(
        is_supported=False,
        hallucinated_elements=["officially migrated"],
        rejection_reason="The text says 'might look into', not officially migrated.",
    )

    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = mock_structured_llm

    res = await verify_extraction(seg, payload, llm_client=mock_llm)
    assert res.is_supported is False
    assert "officially migrated" in res.hallucinated_elements


@pytest.mark.asyncio
async def test_verify_extraction_retries_once_then_succeeds() -> None:
    """A first-attempt validation failure is not fatal: verify_extraction
    retries and returns the recovered result, not the generic
    'LLM verification exception' rejection.
    """
    seg = make_dummy_segment("Alice: We chose Postgres for database isolation.")
    payload = {"decision_title": "Use Postgres"}

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.side_effect = [
        ValueError("schema validation failed: invalid output"),
        VerifiedExtraction(is_supported=True),
    ]

    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = mock_structured_llm

    res = await verify_extraction(seg, payload, llm_client=mock_llm)
    assert res.is_supported is True
    assert mock_structured_llm.ainvoke.call_count == 2


@pytest.mark.asyncio
async def test_verify_extraction_exhausts_retries_then_rejects() -> None:
    """A systematically wrong schema exhausts the cap (3 total attempts) and
    then falls through to the rejection path.
    """
    seg = make_dummy_segment("Alice: Test text.")
    payload = {"decision": "Test"}

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.side_effect = RuntimeError("persistently invalid schema")

    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = mock_structured_llm

    res = await verify_extraction(seg, payload, llm_client=mock_llm)
    assert res.is_supported is False
    assert "LLM verification exception" in (res.rejection_reason or "")
    assert mock_structured_llm.ainvoke.call_count == 3  # initial attempt + 2 retries


@pytest.mark.asyncio
async def test_verify_extraction_llm_exception() -> None:
    seg = make_dummy_segment("Alice: Test text.")
    payload = {"decision": "Test"}

    mock_structured_llm = AsyncMock()
    mock_structured_llm.ainvoke.side_effect = RuntimeError("OpenAI error")

    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = mock_structured_llm

    res = await verify_extraction(seg, payload, llm_client=mock_llm)
    assert res.is_supported is False
    assert "LLM verification exception" in (res.rejection_reason or "")
