import json
from typing import Any

# Token fields a stream message must not carry; they are dropped, never read or re-published.
LEGACY_TOKEN_FIELDS = frozenset({"personal_access_token", "pat"})
_JSON_FIELDS = ("payload", "data")


def scrub_tokens(fields: dict[str, Any]) -> dict[str, Any]:
    """Copy of a flat stream message without token fields, including inside its JSON body."""
    clean = {k: v for k, v in fields.items() if k not in LEGACY_TOKEN_FIELDS}
    for key in _JSON_FIELDS:
        body = clean.get(key)
        if not isinstance(body, str):
            continue
        try:
            decoded = json.loads(body)
        except ValueError:
            continue
        if isinstance(decoded, dict) and LEGACY_TOKEN_FIELDS & decoded.keys():
            clean[key] = json.dumps(
                {k: v for k, v in decoded.items() if k not in LEGACY_TOKEN_FIELDS}
            )
    return clean
