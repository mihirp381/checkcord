"""Bot Framework bearer-token validation for inbound Teams activities."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import jwt
import structlog

from checkcord.core.config import get_settings

logger = structlog.get_logger(__name__)

BOT_FRAMEWORK_ISSUER = "https://api.botframework.com"
CLOCK_SKEW_SECONDS = 300
KEYS_TTL_SECONDS = 24 * 3600
MIN_REFRESH_SECONDS = 300


class BotAuthError(Exception):
    pass


async def fetch_json(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data


class SigningKeys:
    def __init__(
        self,
        metadata_url: str | None = None,
        fetch: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.metadata_url = metadata_url
        self.fetch = fetch or fetch_json
        self._keys: dict[str, dict[str, Any]] = {}
        self._fetched_at = 0.0
        self.issuer = BOT_FRAMEWORK_ISSUER

    async def _refresh(self) -> None:
        metadata = await self.fetch(self.metadata_url or get_settings().botframework_openid_url)
        self.issuer = str(metadata.get("issuer") or BOT_FRAMEWORK_ISSUER)
        jwks = await self.fetch(str(metadata["jwks_uri"]))
        self._keys = {k["kid"]: k for k in jwks.get("keys", []) if k.get("kid")}
        self._fetched_at = time.monotonic()

    async def get(self, kid: str) -> dict[str, Any] | None:
        age = time.monotonic() - self._fetched_at
        stale = not self._fetched_at or age > KEYS_TTL_SECONDS
        # Unknown kid may mean rotation; refetch, but not on every forged token.
        if stale or (kid not in self._keys and age > MIN_REFRESH_SECONDS):
            await self._refresh()
        return self._keys.get(kid)

    def clear(self) -> None:
        self._keys = {}
        self._fetched_at = 0.0


signing_keys = SigningKeys()


async def verify_bot_token(
    authorization: str | None,
    activity: dict[str, Any],
    app_id: str | None,
    keys: SigningKeys | None = None,
) -> dict[str, Any]:
    if not app_id:
        raise BotAuthError("bot app id is not configured")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise BotAuthError("missing bearer token")
    token = authorization[7:].strip()
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError as err:
        raise BotAuthError("malformed token") from err
    if not kid:
        raise BotAuthError("token has no key id")

    keys = keys or signing_keys
    try:
        jwk = await keys.get(str(kid))
    except Exception as err:
        logger.warning("bot_framework_keys_unavailable", error=str(err))
        raise BotAuthError("signing keys unavailable") from err
    if jwk is None:
        raise BotAuthError("unknown signing key")

    channel_id = activity.get("channelId")
    endorsements = jwk.get("endorsements")
    if channel_id and endorsements is not None and channel_id not in endorsements:
        raise BotAuthError("signing key is not endorsed for this channel")

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key=jwt.PyJWK(jwk).key,
            algorithms=["RS256"],
            audience=app_id,
            issuer=keys.issuer,
            leeway=CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as err:
        raise BotAuthError(f"invalid token: {err}") from err

    service_url = activity.get("serviceUrl")
    if not service_url or claims.get("serviceUrl") != service_url:
        raise BotAuthError("serviceUrl claim does not match the activity")
    return claims
