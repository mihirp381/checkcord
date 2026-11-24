"""The model pass gives back code identifiers it redacted; secrets, contacts and names stay."""

import base64
import json
import random
import string
from unittest.mock import AsyncMock, MagicMock

import pytest

from checkcord.pipeline.redaction import (
    RedactedEntity,
    RedactionOutput,
    is_code_name,
    redact_for_storage,
    restore_code_names,
)

pytestmark = pytest.mark.asyncio

_rng = random.Random(7)


def _letters(n: int, alphabet: str = string.ascii_letters + string.digits) -> str:
    return "".join(_rng.choice(alphabet) for _ in range(n))


def _jwt() -> str:
    def part(value: dict[str, str]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return ".".join([part({"alg": "HS256"}), part({"sub": _letters(12)}), _letters(32)])


def _model(spans: list[str]) -> MagicMock:
    structured = AsyncMock()
    structured.ainvoke.return_value = RedactionOutput(
        entities=[RedactedEntity(text=s, entity_type="TOKEN") for s in spans]
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


@pytest.mark.parametrize("name", ["rotate_webhook_signer", "CarrierClient", "fetchLiveQuote"])
async def test_a_redacted_code_name_is_restored(name: str) -> None:
    text = f"It was rotated with {name}; notes stay in #security-review."
    stored = await redact_for_storage(text, llm_client=_model([name]))
    assert stored.text == text


@pytest.mark.parametrize(
    "secret",
    [
        "sk_" + "live_" + _letters(24),
        "sk_" + "live_" + _letters(24, string.ascii_letters),
        "AKIA" + _letters(16, string.ascii_uppercase + string.digits),
        _jwt(),
        "carol" + "@" + "acme.example",
    ],
)
async def test_secrets_contacts_and_names_stay_redacted(secret: str) -> None:
    text = f"The value {secret} was pasted into a ticket."
    stored = await redact_for_storage(text, llm_client=_model([secret]))
    assert secret not in stored.text
    assert "[REDACTED:" in stored.text


async def test_ambiguous_alignment_keeps_the_redaction() -> None:
    original = "use CarrierClient CarrierClient here"
    redacted = "use [REDACTED:TOKEN] here"
    assert restore_code_names(original, redacted) == redacted
    assert restore_code_names("a b", "[REDACTED:TOKEN][REDACTED:TOKEN] b") == (
        "[REDACTED:TOKEN][REDACTED:TOKEN] b"
    )


async def test_restoring_keeps_other_redactions_and_spacing() -> None:
    original = "call  fetch_live_quote,\nthen mail ops@acme.example"
    redacted = "call  [REDACTED:TOKEN],\nthen mail [REDACTED:EMAIL]"
    assert restore_code_names(original, redacted) == (
        "call  fetch_live_quote,\nthen mail [REDACTED:EMAIL]"
    )


async def test_code_name_rule() -> None:
    assert is_code_name("verify_webhook_signature")
    assert not is_code_name("rotate_signer_v2")
    assert not is_code_name("sk_live_abcdefgh")
    assert not is_code_name("ab_XqZrTyWb_Kp")
    assert not is_code_name("plainword")


def _labelled(spans: list[tuple[str, str]]) -> MagicMock:
    structured = AsyncMock()
    structured.ainvoke.return_value = RedactionOutput(
        entities=[RedactedEntity(text=s, entity_type=t) for s, t in spans]
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


@pytest.mark.parametrize("span", ["payments-api", "on-call team"])
async def test_harmless_words_are_not_redacted(span: str) -> None:
    text = "The on-call team owns payments-api escalations."
    stored = await redact_for_storage(text, llm_client=_labelled([(span, "EMAIL")]))
    assert stored.text == text


async def test_a_harmless_looking_secret_labelled_token_stays_redacted() -> None:
    text = "The staging password is tulip-harbor-42."
    stored = await redact_for_storage(text, llm_client=_labelled([("tulip-harbor-42", "TOKEN")]))
    assert "tulip-harbor-42" not in stored.text


async def test_an_email_stays_redacted() -> None:
    email = "oncall" + "@" + "acme.example"
    text = f"Page {email} for payments-api."
    stored = await redact_for_storage(text, llm_client=_labelled([(email, "EMAIL")]))
    assert email not in stored.text
    assert "payments-api" in stored.text


@pytest.mark.parametrize("span", ["90 days", "24 hours, 3 times"])
async def test_a_short_number_labelled_phone_is_not_redacted(span: str) -> None:
    text = f"Session tokens expire after {span}."
    stored = await redact_for_storage(text, llm_client=_labelled([(span, "PHONE")]))
    assert stored.text == text


async def test_a_phone_number_stays_redacted() -> None:
    text = "Call the bank desk on +49 30 1234 5678."
    stored = await redact_for_storage(text, llm_client=_labelled([("+49 30 1234 5678", "PHONE")]))
    assert "1234 5678" not in stored.text
