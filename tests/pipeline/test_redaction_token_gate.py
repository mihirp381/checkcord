import random
import string
from unittest.mock import AsyncMock, MagicMock

import pytest

from checkcord.pipeline.redaction import (
    RedactedEntity,
    RedactionOutput,
    cue_before,
    redact_for_storage,
    shape_secret,
)

_rng = random.Random(11)


def _model(spans: list[str]) -> MagicMock:
    structured = AsyncMock()
    structured.ainvoke.return_value = RedactionOutput(
        entities=[RedactedEntity(text=s, entity_type="TOKEN") for s in spans]
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


async def _stored(text: str, spans: list[str]) -> str:
    return (await redact_for_storage(text, llm_client=_model(spans))).text


@pytest.mark.asyncio
async def test_service_and_header_names_are_kept() -> None:
    text = "Every POST endpoint in payments-api takes an Idempotency-Key header."
    assert await _stored(text, ["payments-api", "Idempotency-Key"]) == text


@pytest.mark.asyncio
async def test_ticket_key_is_kept() -> None:
    text = "Ticket Created: OPS-7 - Deploy approvals"
    assert await _stored(text, ["OPS-7"]) == text


@pytest.mark.asyncio
async def test_alert_url_without_credentials_is_kept() -> None:
    url = "https://alerts.example.com/incidents/4821?team=payments&severity=high"
    text = f"Alert fired: {url}"
    assert await _stored(text, [url]) == text


@pytest.mark.asyncio
async def test_word_after_password_is_redacted() -> None:
    word = "".join(_rng.choice(string.ascii_lowercase) for _ in range(9))
    text = "the vault password is " + word
    assert await _stored(text, [word]) == "the vault password is [REDACTED:TOKEN]"


@pytest.mark.asyncio
async def test_long_mixed_string_without_cue_is_redacted() -> None:
    alphabet = string.ascii_letters + string.digits
    span = "".join(_rng.choice(alphabet) for _ in range(38)) + "a1"
    text = f"Use {span} for the staging box."
    assert await _stored(text, [span]) == "Use [REDACTED:TOKEN] for the staging box."


@pytest.mark.asyncio
async def test_value_after_key_is_is_redacted() -> None:
    assert await _stored("the deploy key is abc", ["abc"]) == "the deploy key is [REDACTED:TOKEN]"


def test_shape_rules() -> None:
    assert shape_secret("https://bot:hunter@hooks.example.com/x")
    assert shape_secret("https://api.example.com/v1?api_key=abc")
    assert not shape_secret("https://grafana.example.com/d/abc123?orgId=1")
    assert not shape_secret("v1.24.3")
    assert not shape_secret("PAY-12345678901234")
    assert not shape_secret("3f2b9c1d0e4a5b6c7d8e9f00")
    assert not shape_secret("payments api 2024 rollout plan")
    assert cue_before("bearer abc", "abc")
    assert not cue_before("the key part abc", "abc")


def test_uuid_and_hex_are_secrets_only_after_a_cue() -> None:
    uuid = "9F2B9C1D-0E4A-4B6C-8D8E-9F00A1B2C3D4"
    hex_id = "3f2b9c1d0e4a5b6c7d8e9f00a1b2c3d4e5f60718"
    for span in (uuid, uuid.lower(), hex_id, hex_id.upper()):
        assert not shape_secret(span)
        assert cue_before(f"the api token is {span}", span)
