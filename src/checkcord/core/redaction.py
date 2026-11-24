"""The regex redaction pass, and serving stored redacted text.

Redacted text is computed when a record is stored (``pipeline/redaction.py``).
Read paths use only this module: it never calls a model. Every value served
gets the regex pass, so a row without stored redaction is never served raw.
``EMAIL``, ``TOKEN``, ``ACCOUNT`` and ``PHONE`` shapes are redacted, and sentences
that instruct an AI agent are removed (``core/agent_text.py``). Names are kept on
purpose: who decided something is part of the record.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from checkcord.core.agent_text import strip_instructions
from checkcord.core.secret_patterns import TOKEN_PATTERNS as _TOKEN_PATTERNS

# Bump when the regex pass or the model prompt changes; stored rows on an older
# version are re-run by `checkcord redaction recompute`.
RULES_VERSION = "5"

_EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_PATTERNS = [
    re.compile(r"(?<![\w+])\+\d{1,3}(?:[ .-]?\(?\d{1,4}\)?){2,6}(?![\w-]|\.\d)"),
    re.compile(r"(?<![\w.-])(?:\(\d{3}\) ?|\d{3}[-. ])\d{3}[-. ]\d{4}(?![\w-]|\.\d)"),
    re.compile(r"(?<![\w.-])0\d{2,4} \d{3,4} ?\d{3,4}(?![\w-]|\.\d)"),
]
_ACCOUNT_LABELLED = re.compile(
    r"(?i)(\b(?:account|acct|a/c|iban|customer\s+(?:no|number|id)|card|routing|sort\s+code"
    r"|ssn|passport)\b[^\d\n]{0,20}?)(?!\d{4}-\d{2}-\d{2}(?!\d))(\d(?:[ -]?\d){5,})(?!\d)"
)
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b")
_IBAN_MIN_DIGITS = 10
_PHONE_MIN_DIGITS = 9
_PHONE_MAX_DIGITS = 15


@dataclass(frozen=True)
class StoredRedaction:
    """Redacted text to store next to the original. ``pending``: the model pass failed."""

    text: str
    version: str
    pending: bool = False


def _redact_phone(match: re.Match[str]) -> str:
    digits = sum(c.isdigit() for c in match.group())
    if _PHONE_MIN_DIGITS <= digits <= _PHONE_MAX_DIGITS:
        return "[REDACTED:PHONE]"
    return match.group()


def _redact_iban(match: re.Match[str]) -> str:
    if sum(c.isdigit() for c in match.group()) >= _IBAN_MIN_DIGITS:
        return "[REDACTED:ACCOUNT]"
    return match.group()


def redact_regex(text: str | None) -> str:
    if not text:
        return text or ""
    redacted, _ = strip_instructions(text)
    redacted = _EMAIL_PATTERN.sub("[REDACTED:EMAIL]", redacted)
    for pattern in _TOKEN_PATTERNS:
        redacted = pattern.sub("[REDACTED:TOKEN]", redacted)
    redacted = _IBAN.sub(_redact_iban, redacted)
    redacted = _ACCOUNT_LABELLED.sub(r"\1[REDACTED:ACCOUNT]", redacted)
    for pattern in _PHONE_PATTERNS:
        redacted = pattern.sub(_redact_phone, redacted)
    return redacted


def serve(raw: str | None, stored: str | None = None) -> str:
    """The text to serve: the stored redaction if there is one, always regex-redacted."""
    return redact_regex(stored if stored is not None else raw)


def serve_field(
    rows: Iterable[dict[str, Any]], field: str = "summary", stored: str = "summary_redacted"
) -> tuple[list[dict[str, Any]], int]:
    """Rows with ``field`` served redacted and ``stored`` dropped; also how many differ."""
    out, changed = [], 0
    for row in rows:
        raw = row.get(field) or ""
        text = serve(raw, row.get(stored))
        changed += text != raw
        out.append({k: v for k, v in row.items() if k != stored} | {field: text})
    return out, changed
