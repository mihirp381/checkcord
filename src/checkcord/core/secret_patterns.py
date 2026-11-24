import re

TOKEN_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),  # OpenAI-style secret key
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),  # GitHub PAT/token variants
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),  # Slack token
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-_.]{20,}\b"),  # Generic bearer token
    re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    ),  # PEM private key block
    re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"),  # GCP API key
]


def redact_secrets(text: str) -> str:
    """Replace API keys, tokens and private keys in operational text such as error messages."""
    for pattern in TOKEN_PATTERNS:
        text = pattern.sub("[REDACTED:TOKEN]", text)
    return text
