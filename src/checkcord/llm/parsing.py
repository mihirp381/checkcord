"""Tolerant parsing of plain chat-model replies, for models that add reasoning or prose."""

import json
import re
from collections.abc import Sequence
from typing import Any

_REASONING = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
_UNCLOSED = re.compile(r"<(think|thinking|reasoning)>.*", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """Drop ``<think>`` blocks; an unclosed one means the reply has no answer yet."""
    return _UNCLOSED.sub("", _REASONING.sub("", text)).strip()


def parse_verdict(text: str, yes: str, no: str) -> bool | None:
    """True or False when exactly one of the two words appears; None when neither or both do."""
    words = set(re.findall(r"[A-Z]+", strip_reasoning(text).upper()))
    found = words & {yes, no}
    if len(found) != 1:
        return None
    return found == {yes}


def parse_label(text: str, labels: Sequence[str]) -> str | None:
    """The one label that appears as a whole word; None when none or several do."""
    words = set(re.findall(r"[A-Z]+", strip_reasoning(text).upper()))
    found = words & {label.upper() for label in labels}
    if len(found) != 1:
        return None
    (word,) = found
    return next(label for label in labels if label.upper() == word)


def extract_json_object(text: str) -> dict[str, Any]:
    """The first JSON object in the reply, ignoring fences, reasoning and surrounding prose."""
    text = strip_reasoning(text)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError(f"no JSON object in model reply: {text[:200]!r}")
