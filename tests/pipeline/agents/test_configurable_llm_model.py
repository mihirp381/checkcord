"""Verify MEETING_EXTRACTION_LLM_MODEL reaches every ChatOpenAI(...) construction site
used by the meeting-transcript multi-agent pipeline.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from checkcord.core.config import Settings
from checkcord.llm import factory
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.models.extractions import DiscourseSegment
from checkcord.pipeline import redaction
from checkcord.pipeline.agents import judge, preprocessor, specialists

pytestmark = pytest.mark.asyncio

CUSTOM_MODEL = "custom-test-model-xyz"
CI_MODEL = "custom-ci-model"
REDACTION_MODEL = "custom-redaction-model"


def _make_segment() -> DiscourseSegment:
    return DiscourseSegment(
        parent_event_id=uuid4(),
        tenant_id=uuid4(),
        text_content="Alice: We decided on Postgres.",
    )


def _make_meeting_event() -> CanonicalEvent:
    return CanonicalEvent(
        tenant_id=uuid4(),
        source_platform=SourcePlatform.ZOOM,
        source_type=SourceType.MEETING_TRANSCRIPT,
        source_id="zoom_mtg_config_test",
        happened_at=datetime.now(UTC),
        author=AuthorInfo(source_id="host_1"),
        content_raw="Alice: We decided on Postgres.",
        content_clean="Alice: We decided on Postgres.",
        access_control=AccessControl(is_public=True),
    )


@pytest.fixture(autouse=True)
def _patched_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    custom_settings = Settings(
        meeting_extraction_llm_model=CUSTOM_MODEL,
        ci_failure_extraction_llm_model=CI_MODEL,
        redaction_llm_model=REDACTION_MODEL,
    )
    monkeypatch.setattr(specialists, "get_settings", lambda: custom_settings)
    monkeypatch.setattr(preprocessor, "get_settings", lambda: custom_settings)
    monkeypatch.setattr(judge, "get_settings", lambda: custom_settings)
    monkeypatch.setattr(redaction, "get_settings", lambda: custom_settings)


async def test_extract_adr_uses_configured_model(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_chat_openai = MagicMock()
    monkeypatch.setattr(factory, "ChatOpenAI", mock_chat_openai)

    await specialists.extract_adr(_make_segment(), llm_client=None)

    assert mock_chat_openai.call_args.kwargs["model_name"] == CUSTOM_MODEL


async def test_extract_constraints_uses_configured_model(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_chat_openai = MagicMock()
    monkeypatch.setattr(factory, "ChatOpenAI", mock_chat_openai)

    await specialists.extract_constraints(_make_segment(), llm_client=None)

    assert mock_chat_openai.call_args.kwargs["model_name"] == CUSTOM_MODEL


async def test_extract_risks_uses_configured_model(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_chat_openai = MagicMock()
    monkeypatch.setattr(factory, "ChatOpenAI", mock_chat_openai)

    await specialists.extract_risks(_make_segment(), llm_client=None)

    assert mock_chat_openai.call_args.kwargs["model_name"] == CUSTOM_MODEL


async def test_segment_transcript_uses_configured_model(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_chat_openai = MagicMock()
    monkeypatch.setattr(factory, "ChatOpenAI", mock_chat_openai)

    await preprocessor.segment_transcript(_make_meeting_event(), llm_client=None)

    assert mock_chat_openai.call_args.kwargs["model_name"] == CUSTOM_MODEL


async def test_verify_extraction_uses_configured_model(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_chat_openai = MagicMock()
    monkeypatch.setattr(factory, "ChatOpenAI", mock_chat_openai)

    await judge.verify_extraction(_make_segment(), {"decision_title": "x"}, llm_client=None)

    assert mock_chat_openai.call_args.kwargs["model_name"] == CUSTOM_MODEL


async def test_extract_ci_failure_uses_configured_model(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_chat_openai = MagicMock()
    monkeypatch.setattr(factory, "ChatOpenAI", mock_chat_openai)

    await specialists.extract_ci_failure(_make_segment(), llm_client=None)

    assert mock_chat_openai.call_args.kwargs["model_name"] == CI_MODEL


async def test_redaction_uses_configured_model(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_chat_openai = MagicMock()
    monkeypatch.setattr(factory, "ChatOpenAI", mock_chat_openai)

    await redaction.redact_text("contact alice@example.com about the uniquely-worded rollout")

    assert mock_chat_openai.call_args.kwargs["model_name"] == REDACTION_MODEL
