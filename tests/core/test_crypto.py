import hashlib

import pytest
from cryptography.fernet import Fernet

from checkcord.core.crypto import (
    SecretDecryptionError,
    decrypt_secret,
    decrypt_token,
    encrypt_secret,
    encrypt_token,
    hash_api_key,
    is_encrypted,
)


def test_token_encryption_roundtrip():
    token = "secret_bearer_token_12345"
    cipher = encrypt_token(token)
    assert cipher != token
    assert decrypt_token(cipher) == token


def test_empty_token_encryption():
    assert encrypt_token("") == ""
    assert decrypt_token("") == ""


def test_hash_api_key():
    token = "  test_key_abc  "
    hashed = hash_api_key(token)
    expected = hashlib.sha256(b"test_key_abc").hexdigest()
    assert hashed == expected


def test_decrypt_secret_roundtrip_and_legacy_plaintext() -> None:
    assert decrypt_secret(encrypt_secret("ghp_abc")) == "ghp_abc"
    assert decrypt_secret("ghp_legacy_plaintext") == "ghp_legacy_plaintext"
    assert decrypt_secret("") == ""
    assert decrypt_secret(None) == ""


def test_ciphertext_under_another_key_raises_naming_the_key() -> None:
    foreign = Fernet(Fernet.generate_key()).encrypt(b"ghp_abc").decode()
    assert is_encrypted(foreign)
    with pytest.raises(SecretDecryptionError, match="FERNET_MASTER_KEY"):
        decrypt_secret(foreign)


@pytest.mark.parametrize(
    "value",
    ["ghp_abc", "xoxb-1-2-3", "gAAAAA", "not base64 !!", "a" * 120, "é" * 100],
)
def test_non_fernet_values_are_not_encrypted(value: str) -> None:
    assert not is_encrypted(value)


def test_fernet_values_are_encrypted() -> None:
    assert is_encrypted(encrypt_secret("x"))
    assert is_encrypted(encrypt_secret("x" * 500))
    assert is_encrypted("")
