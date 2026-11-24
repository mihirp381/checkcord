import re
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Compile highly targeted regex patterns for fast matching to prevent over-matching
# (fidelity corruption) and avoid catastrophic backtracking.
TIME_PATTERNS = [
    # Today is Saturday, July 4, 2026 or Today is Sunday
    re.compile(
        r"(?i)\bToday is (?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
        r"(?:,?\s+\w+\s+\d{1,2},?\s+\d{4})?\.?"
    ),
    # The current time is 9:40 AM
    re.compile(r"(?i)\bThe current time is \d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?\.?"),
    # Local time: 2026-07-04 09:40:15
    re.compile(
        r"(?i)\bLocal time\s*:\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\s*[-+]\d{2}:?\d{2})?\.?"
    ),
    # Date: 2026-07-04 or Date: July 4, 2026
    re.compile(r"(?i)\bDate\s*:\s*(?:\d{4}-\d{2}-\d{2}|\w+\s+\d{1,2},?\s+\d{4})\.?"),
    # Current time: 09:40:15
    re.compile(
        r"(?i)\bCurrent time\s*:\s*\d{1,2}:\d{2}(?::\d{2})?(?:\s*[A-Z]{3,4})?(?:\s*(?:AM|PM))?\.?"
    ),
]

OS_PATTERNS = [
    # Operating System: macOS 14.5 (Darwin) or OS: Windows 11
    re.compile(
        r"(?i)\b(?:Operating System|OS|Platform)\s*:\s*(?:macOS|OSX|Darwin|Windows|"
        r"Linux|Ubuntu|CentOS|Debian|RedHat|Android|iOS|Unix|POSIX)(?:\s+\d+(?:\.\d+)*)?"
        r"(?:\s*\([^)]+\))?\.?"
    ),
]

PATH_PATTERNS = [
    # Working directory: /Users/alex/project or cwd: C:\Users\alex\project
    re.compile(
        r"(?i)\b(?:Working directory|Workspace|cwd)\s*:\s*(?:/[^\s\r\n\.]+|\\\\|"
        r"[A-Za-z]:\\[^\s\r\n\.]+)[^\s\r\n]*\.?"
    ),
]


def sanitize_text(text: str) -> str:
    """Sanitize volatile dynamic metadata from a given string."""
    if not isinstance(text, str):
        return text

    original = text
    for pattern in TIME_PATTERNS:
        text = pattern.sub("[TIME_REDACTED]", text)
    for pattern in OS_PATTERNS:
        text = pattern.sub("[OS_REDACTED]", text)
    for pattern in PATH_PATTERNS:
        text = pattern.sub("[PATH_REDACTED]", text)

    if text != original:
        logger.debug("sanitizer_redacted_text", original_len=len(original), new_len=len(text))
    return text


def sanitize_content_field(content: Any) -> Any:
    """Sanitizes the content field of a message block (can be str or list of dicts)."""
    if isinstance(content, str):
        return sanitize_text(content)
    elif isinstance(content, list):
        sanitized_list = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and "text" in block:
                # Clone block to avoid modifying input references in-place
                new_block = dict(block)
                new_block["text"] = sanitize_text(block["text"])
                sanitized_list.append(new_block)
            else:
                sanitized_list.append(block)
        return sanitized_list
    return content


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Sanitizes outgoing LLM API payload, editing only system prompt metadata.

    Guarantees that user or assistant messages are never altered.
    Supports both OpenAI (messages: [{role: system, content}]) and
    Anthropic (system: str/list, messages) formats.
    """
    if not isinstance(payload, dict):
        return payload

    # Copy the dictionary shallowly so we don't cause side-effects on original references
    mutated = dict(payload)

    # Anthropic Top-Level System Prompt
    if "system" in mutated:
        system_val = mutated["system"]
        if isinstance(system_val, str):
            mutated["system"] = sanitize_text(system_val)
        elif isinstance(system_val, list):
            sanitized_list = []
            for block in system_val:
                if isinstance(block, dict) and block.get("type") == "text" and "text" in block:
                    new_block = dict(block)
                    new_block["text"] = sanitize_text(block["text"])
                    sanitized_list.append(new_block)
                else:
                    sanitized_list.append(block)
            mutated["system"] = sanitized_list

    # OpenAI (and Anthropic fallback) Messages List
    if "messages" in mutated and isinstance(mutated["messages"], list):
        sanitized_messages = []
        for msg in mutated["messages"]:
            if isinstance(msg, dict) and msg.get("role") == "system":
                # Only sanitize system messages
                new_msg = dict(msg)
                if "content" in new_msg:
                    new_msg["content"] = sanitize_content_field(new_msg["content"])
                sanitized_messages.append(new_msg)
            else:
                # Fidelity guarantee: append other messages unmodified
                sanitized_messages.append(msg)
        mutated["messages"] = sanitized_messages

    return mutated
