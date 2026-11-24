"""Encryption of stored secrets and hashing of API keys."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from checkcord.core.config import get_settings

# Default development key (for non-prod environments when FERNET_MASTER_KEY is not set)
DEFAULT_DEV_FERNET_KEY = base64.urlsafe_b64encode(
    hashlib.sha256(b"checkcord_dev_key").digest()
).decode()

_DEV_TEST_ENVIRONMENTS = ("development", "test")


def _get_fernet_instance(key: str | None = None) -> Fernet:
    """Helper to retrieve Fernet instance from key or environment.

    Reads the key through Settings (the actual source of truth for .env-loaded
    config) rather than os.getenv directly. Outside development/test, a real
    key is required -- falling back to the well-known dev key in that context
    would silently encrypt customer secrets with a key checked into source
    control.
    """
    settings = get_settings()
    if not key:
        key = settings.fernet_master_key
    if not key:
        if settings.environment not in _DEV_TEST_ENVIRONMENTS:
            raise RuntimeError(
                "FERNET_MASTER_KEY is not configured. A real Fernet master key is "
                f"required outside development/test (environment={settings.environment!r})."
            )
        key = DEFAULT_DEV_FERNET_KEY

    # Ensure key is in bytes
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    return Fernet(key_bytes)


def encrypt_token(plain_text: str, key: str | None = None) -> str:
    """Encrypt a plain text secret or token using Fernet symmetric encryption.

    Returns the URL-safe base64 encoded ciphertext string.
    """
    if not plain_text:
        return ""
    fernet = _get_fernet_instance(key)
    ciphertext = fernet.encrypt(plain_text.encode("utf-8"))
    return ciphertext.decode("utf-8")


def decrypt_token(cipher_text: str, key: str | None = None) -> str:
    """Decrypt a Fernet ciphertext back to its plain text representation."""
    if not cipher_text:
        return ""
    fernet = _get_fernet_instance(key)
    plaintext = fernet.decrypt(cipher_text.encode("utf-8"))
    return plaintext.decode("utf-8")


def encrypt_secret(value: str | None) -> str:
    """Encrypt a stored connector credential; empty stays empty."""
    return encrypt_token(value or "")


class SecretDecryptionError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "A stored credential is encrypted but does not decrypt with FERNET_MASTER_KEY; "
            "the key is wrong or was rotated. Restore the key that encrypted it."
        )


def _is_fernet_shaped(value: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii") + b"=" * (-len(value) % 4))
    except (ValueError, UnicodeEncodeError):
        return False
    # version byte, 8-byte timestamp, 16-byte IV, AES blocks, 32-byte HMAC
    return len(raw) >= 73 and raw[0] == 0x80 and (len(raw) - 57) % 16 == 0


def decrypt_secret(stored: str | None) -> str:
    """Decrypt a stored credential; values without the Fernet shape are plaintext."""
    if not stored:
        return ""
    if not _is_fernet_shaped(stored):
        return stored
    try:
        return decrypt_token(stored)
    except InvalidToken as err:
        raise SecretDecryptionError() from err


def is_encrypted(stored: str | None) -> bool:
    return not stored or _is_fernet_shaped(stored)


def hash_api_key(token: str) -> str:
    """Generate SHA-256 hex digest for an API token before DB lookup/storage."""
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
