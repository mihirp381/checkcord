"""Signed, expiring, single-use OAuth ``state`` values that carry the tenant."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from uuid import UUID

from checkcord.core.config import get_settings
from checkcord.core.crypto import DEFAULT_DEV_FERNET_KEY

STATE_TTL_SECONDS = 900
PROVIDERS = ("slack", "teams", "notion", "linear")
INSTALL_PATHS = {
    "slack": "/slack/install",
    "teams": "/msteams/install",
    "notion": "/api/v1/webhooks/notion/install",
    "linear": "/api/v1/webhooks/linear/install",
}
_USED_KEY = "oauth_state:used:{}"


class InvalidStateError(Exception):
    pass


@dataclass(frozen=True)
class OAuthState:
    provider: str
    tenant_id: UUID
    nonce: str
    expires_at: int


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _signing_key() -> bytes:
    settings = get_settings()
    secret = settings.oauth_state_secret or settings.fernet_master_key
    if not secret:
        if settings.environment not in ("development", "test"):
            raise RuntimeError("OAUTH_STATE_SECRET or FERNET_MASTER_KEY must be set")
        secret = DEFAULT_DEV_FERNET_KEY
    return hmac.new(secret.encode(), b"checkcord-oauth-state", hashlib.sha256).digest()


def _sign(body: str) -> str:
    return _b64(hmac.new(_signing_key(), body.encode(), hashlib.sha256).digest())


def mint_state(
    provider: str, tenant_id: UUID, ttl: int = STATE_TTL_SECONDS, now: float | None = None
) -> str:
    if provider not in PROVIDERS:
        raise ValueError(provider)
    claims = {
        "p": provider,
        "t": str(tenant_id),
        "n": secrets.token_urlsafe(16),
        "e": int((now if now is not None else time.time()) + ttl),
    }
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    return f"{body}.{_sign(body)}"


def read_state(token: str | None, provider: str, now: float | None = None) -> OAuthState:
    if not token or token.count(".") != 1:
        raise InvalidStateError("missing or malformed state")
    body, signature = token.split(".")
    if not hmac.compare_digest(_sign(body), signature):
        raise InvalidStateError("bad state signature")
    try:
        claims: dict[str, Any] = json.loads(_unb64(body))
        state = OAuthState(
            provider=str(claims["p"]),
            tenant_id=UUID(str(claims["t"])),
            nonce=str(claims["n"]),
            expires_at=int(claims["e"]),
        )
    except (ValueError, KeyError, TypeError, binascii.Error) as err:
        raise InvalidStateError("malformed state") from err
    if state.provider != provider:
        raise InvalidStateError("state was issued for another provider")
    if state.expires_at < (now if now is not None else time.time()):
        raise InvalidStateError("state expired")
    return state


async def consume_state(redis: Any, token: str | None, provider: str) -> OAuthState:
    state = read_state(token, provider)
    if redis is None:
        raise InvalidStateError("state store unavailable")
    ttl = max(1, state.expires_at - int(time.time()))
    if not await redis.set(_USED_KEY.format(state.nonce), "1", nx=True, ex=ttl):
        raise InvalidStateError("state already used")
    return state


def install_url(base_url: str, provider: str, tenant_id: UUID) -> str:
    state = mint_state(provider, tenant_id)
    return f"{base_url.rstrip('/')}{INSTALL_PATHS[provider]}?state={quote(state)}"
