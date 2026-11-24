import base64
import json
import time
from uuid import UUID, uuid4

import fakeredis
import pytest

from checkcord.core import oauth_state
from checkcord.core.config import get_settings
from checkcord.core.oauth_state import (
    STATE_TTL_SECONDS,
    InvalidStateError,
    consume_state,
    install_url,
    mint_state,
    read_state,
)

TENANT = UUID("11111111-2222-3333-4444-555555555555")


def _reencode(state: str, **changes: object) -> str:
    body, signature = state.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims.update(changes)
    raw = json.dumps(claims, separators=(",", ":")).encode()
    return f"{base64.urlsafe_b64encode(raw).rstrip(b'=').decode()}.{signature}"


@pytest.mark.parametrize("provider", oauth_state.PROVIDERS)
def test_round_trip_carries_tenant(provider: str) -> None:
    state = read_state(mint_state(provider, TENANT), provider)
    assert (state.provider, state.tenant_id) == (provider, TENANT)
    assert state.expires_at - time.time() == pytest.approx(STATE_TTL_SECONDS, abs=5)


@pytest.mark.parametrize("token", [None, "", "abc", "a.b.c", str(TENANT), "e30.e30"])
def test_malformed_rejected(token: str | None) -> None:
    with pytest.raises(InvalidStateError):
        read_state(token, "slack")


def test_tampered_tenant_rejected() -> None:
    forged = _reencode(mint_state("slack", TENANT), t=str(uuid4()))
    with pytest.raises(InvalidStateError, match="signature"):
        read_state(forged, "slack")


def test_tampered_expiry_rejected() -> None:
    forged = _reencode(mint_state("slack", TENANT, ttl=-10), e=int(time.time()) + 3600)
    with pytest.raises(InvalidStateError, match="signature"):
        read_state(forged, "slack")


def test_expired_rejected() -> None:
    state = mint_state("linear", TENANT, now=time.time() - STATE_TTL_SECONDS - 1)
    with pytest.raises(InvalidStateError, match="expired"):
        read_state(state, "linear")


def test_expiry_boundary_with_injected_clock() -> None:
    state = mint_state("linear", TENANT, now=1000.0)
    assert read_state(state, "linear", now=1000.0 + STATE_TTL_SECONDS).tenant_id == TENANT
    with pytest.raises(InvalidStateError, match="expired"):
        read_state(state, "linear", now=1000.0 + STATE_TTL_SECONDS + 1)


def test_wrong_provider_rejected() -> None:
    with pytest.raises(InvalidStateError, match="another provider"):
        read_state(mint_state("notion", TENANT), "linear")


def test_other_key_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    state = mint_state("teams", TENANT)
    monkeypatch.setenv("OAUTH_STATE_SECRET", "a-different-secret")
    get_settings.cache_clear()
    with pytest.raises(InvalidStateError, match="signature"):
        read_state(state, "teams")


def test_production_without_key_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("FERNET_MASTER_KEY", raising=False)
    monkeypatch.delenv("OAUTH_STATE_SECRET", raising=False)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError):
        mint_state("slack", TENANT)


@pytest.mark.asyncio
async def test_consume_is_single_use() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    token = mint_state("slack", TENANT)
    assert (await consume_state(redis, token, "slack")).tenant_id == TENANT
    with pytest.raises(InvalidStateError, match="already used"):
        await consume_state(redis, token, "slack")
    assert await consume_state(redis, mint_state("slack", TENANT), "slack")


@pytest.mark.asyncio
async def test_consume_marker_expires_with_state() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    token = mint_state("slack", TENANT)
    state = await consume_state(redis, token, "slack")
    ttl = await redis.ttl(f"oauth_state:used:{state.nonce}")
    assert 0 < ttl <= STATE_TTL_SECONDS


@pytest.mark.asyncio
async def test_consume_without_redis_fails_closed() -> None:
    with pytest.raises(InvalidStateError):
        await consume_state(None, mint_state("slack", TENANT), "slack")


def test_install_url() -> None:
    url = install_url("https://checkcord.example.com/", "notion", TENANT)
    prefix = "https://checkcord.example.com/api/v1/webhooks/notion/install?state="
    assert url.startswith(prefix)
    assert read_state(url.removeprefix(prefix), "notion").tenant_id == TENANT
