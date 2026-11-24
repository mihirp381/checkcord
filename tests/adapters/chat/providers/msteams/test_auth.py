import json
import time
from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from checkcord.adapters.chat.providers.msteams import auth
from checkcord.adapters.chat.providers.msteams.auth import (
    BOT_FRAMEWORK_ISSUER,
    BotAuthError,
    SigningKeys,
    verify_bot_token,
)
from checkcord.core.config import Settings
from checkcord.core.lifecycle import AppContext
from checkcord.main import app

APP_ID = "bot-app-id"
SERVICE_URL = "https://smba.trafficmanager.net/amer/"
JWKS_URI = "https://login.botframework.com/v1/.well-known/keys"
OPENID_METADATA_URL = Settings(environment="test").botframework_openid_url
ACTIVITY = {"type": "message", "channelId": "msteams", "serviceUrl": SERVICE_URL}


def _keypair() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


SIGNER = _keypair()
OTHER = _keypair()


def _jwk(key: rsa.RSAPrivateKey, kid: str, endorsements: list[str] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    data.update(kid=kid, use="sig", alg="RS256")
    if endorsements is not None:
        data["endorsements"] = endorsements
    return data


def _fetcher(*jwks: dict[str, Any]) -> AsyncMock:
    documents = {
        OPENID_METADATA_URL: {"issuer": BOT_FRAMEWORK_ISSUER, "jwks_uri": JWKS_URI},
        JWKS_URI: {"keys": list(jwks)},
    }

    async def fetch(url: str) -> dict[str, Any]:
        return documents[url]

    return AsyncMock(side_effect=fetch)


def _token(key: rsa.RSAPrivateKey = SIGNER, kid: str = "k1", **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": BOT_FRAMEWORK_ISSUER,
        "aud": APP_ID,
        "exp": now + 3600,
        "nbf": now - 10,
        "serviceUrl": SERVICE_URL,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def _keys(*jwks: dict[str, Any]) -> SigningKeys:
    return SigningKeys(fetch=_fetcher(*(jwks or (_jwk(SIGNER, "k1", ["msteams"]),))))


@pytest.mark.asyncio
async def test_valid_token_accepted() -> None:
    claims = await verify_bot_token(f"Bearer {_token()}", ACTIVITY, APP_ID, _keys())
    assert claims["aud"] == APP_ID


@pytest.mark.asyncio
async def test_small_clock_skew_tolerated() -> None:
    token = _token(exp=int(time.time()) - 60)
    assert await verify_bot_token(f"Bearer {token}", ACTIVITY, APP_ID, _keys())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authorization", "reason"),
    [
        (lambda: None, "missing bearer"),
        (lambda: "", "missing bearer"),
        (lambda: "Basic abc", "missing bearer"),
        (lambda: "Bearer not-a-jwt", "malformed"),
        (lambda: f"Bearer {_token(aud='someone-else')}", "Audience"),
        (lambda: f"Bearer {_token(iss='https://sts.windows.net/evil/')}", "issuer"),
        (lambda: f"Bearer {_token(exp=int(time.time()) - 3600)}", "expired"),
        (lambda: f"Bearer {_token(exp=None)}", "exp"),
        (lambda: f"Bearer {_token(key=OTHER)}", "Signature"),
        (lambda: f"Bearer {_token(kid='unknown')}", "unknown signing key"),
        (lambda: f"Bearer {_token(serviceUrl='https://evil.example/')}", "serviceUrl"),
    ],
)
async def test_bad_tokens_rejected(authorization: Callable[[], str | None], reason: str) -> None:
    with pytest.raises(BotAuthError, match=reason):
        await verify_bot_token(authorization(), ACTIVITY, APP_ID, _keys())


@pytest.mark.asyncio
async def test_hs256_token_rejected() -> None:
    token = jwt.encode(
        {"iss": BOT_FRAMEWORK_ISSUER, "aud": APP_ID, "exp": int(time.time()) + 60},
        "shared-secret-long-enough-for-hmac-sha256",
        algorithm="HS256",
        headers={"kid": "k1"},
    )
    with pytest.raises(BotAuthError):
        await verify_bot_token(f"Bearer {token}", ACTIVITY, APP_ID, _keys())


@pytest.mark.asyncio
async def test_unconfigured_app_id_fails_closed() -> None:
    with pytest.raises(BotAuthError, match="not configured"):
        await verify_bot_token(f"Bearer {_token()}", ACTIVITY, None, _keys())


@pytest.mark.asyncio
async def test_key_not_endorsed_for_channel_rejected() -> None:
    keys = _keys(_jwk(SIGNER, "k1", ["skype"]))
    with pytest.raises(BotAuthError, match="endorsed"):
        await verify_bot_token(f"Bearer {_token()}", ACTIVITY, APP_ID, keys)


@pytest.mark.asyncio
async def test_keys_are_cached() -> None:
    keys = _keys()
    for _ in range(3):
        await verify_bot_token(f"Bearer {_token()}", ACTIVITY, APP_ID, keys)
    fetch = keys.fetch
    assert isinstance(fetch, AsyncMock)
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_unknown_kid_refetches_after_cooldown() -> None:
    keys = _keys()
    await verify_bot_token(f"Bearer {_token()}", ACTIVITY, APP_ID, keys)
    keys.fetch = _fetcher(_jwk(SIGNER, "k1", ["msteams"]), _jwk(OTHER, "k2", ["msteams"]))
    with pytest.raises(BotAuthError, match="unknown"):
        await verify_bot_token(f"Bearer {_token(OTHER, 'k2')}", ACTIVITY, APP_ID, keys)
    keys._fetched_at -= auth.MIN_REFRESH_SECONDS + 1
    assert await verify_bot_token(f"Bearer {_token(OTHER, 'k2')}", ACTIVITY, APP_ID, keys)


@pytest.mark.asyncio
async def test_metadata_fetch_failure_rejects() -> None:
    keys = SigningKeys(fetch=AsyncMock(side_effect=OSError("down")))
    with pytest.raises(BotAuthError, match="unavailable"):
        await verify_bot_token(f"Bearer {_token()}", ACTIVITY, APP_ID, keys)


@pytest.fixture
def endpoint() -> Iterator[tuple[TestClient, MagicMock]]:
    ctx = MagicMock(spec=AppContext)
    ctx.settings = MagicMock()
    ctx.settings.msteams_client_id = APP_ID
    ctx.db_engine = MagicMock()
    ctx.redis_client = None

    @asynccontextmanager
    async def lifecycle() -> AsyncGenerator[AppContext, None]:
        yield ctx

    with (
        patch("checkcord.main.app_lifecycle", lifecycle),
        patch.object(auth, "signing_keys", _keys()),
        TestClient(app) as client,
    ):
        yield client, ctx.db_engine


@pytest.mark.real_bot_auth
@pytest.mark.parametrize(
    "headers",
    [
        lambda: {},
        lambda: {"Authorization": f"Bearer {_token(aud='someone-else')}"},
        lambda: {"Authorization": f"Bearer {_token(iss='https://evil.example')}"},
        lambda: {"Authorization": f"Bearer {_token(exp=int(time.time()) - 3600)}"},
        lambda: {"Authorization": f"Bearer {_token(key=OTHER)}"},
    ],
)
def test_endpoint_rejects_before_database(
    endpoint: tuple[TestClient, MagicMock], headers: Callable[[], dict[str, str]]
) -> None:
    client, db_engine = endpoint
    resp = client.post("/api/messages", json={**ACTIVITY, "tenantId": "t1"}, headers=headers())
    assert resp.status_code == 401
    db_engine.begin.assert_not_called()
    db_engine.connect.assert_not_called()


@pytest.mark.real_bot_auth
def test_endpoint_accepts_valid_token(endpoint: tuple[TestClient, MagicMock]) -> None:
    client, _ = endpoint
    resp = client.post(
        "/api/messages",
        json={"type": "conversationUpdate", "channelId": "msteams", "serviceUrl": SERVICE_URL},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert resp.status_code == 200
