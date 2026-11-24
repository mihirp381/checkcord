import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
import pytest_asyncio

from checkcord.core.config import Settings
from checkcord.core.oidc import (
    OIDCClient,
    SignInError,
    email_from_claims,
    groups_from_claims,
    pkce_pair,
)
from sim.world.idp import FakeIdP

REDIRECT = "https://checkcord.test/api/v1/auth/callback"


def _settings(**overrides: Any) -> Settings:
    return Settings(
        environment="test",
        oidc_issuer="https://idp.test",
        oidc_client_id="checkcord-test",
        **overrides,
    )


@pytest_asyncio.fixture
async def idp() -> FakeIdP:
    return FakeIdP()


@pytest_asyncio.fixture
async def client(idp: FakeIdP) -> AsyncIterator[OIDCClient]:
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=idp.app))
    oidc = OIDCClient(_settings(), http=http)
    yield oidc
    await oidc.aclose()


async def _id_token(idp: FakeIdP, oidc: OIDCClient, nonce: str = "n-1") -> tuple[str, str]:
    """Run authorize and token against the fake provider; returns (id_token, nonce)."""
    meta = await oidc.metadata()
    verifier, challenge = pkce_pair()
    url = oidc.authorization_url(meta, REDIRECT, "s-1", nonce, challenge)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=idp.app)) as http:
        res = await http.get(url)
    assert res.status_code == 302
    code = parse_qs(urlsplit(res.headers["location"]).query)["code"][0]
    return await oidc.exchange(code, verifier, REDIRECT), nonce


@pytest.mark.asyncio
async def test_valid_token_gives_its_claims(idp: FakeIdP, client: OIDCClient) -> None:
    token, nonce = await _id_token(idp, client)
    claims = await client.validate(token, nonce)
    assert claims["email"] == "person@example.com" and claims["iss"] == "https://idp.test"


@pytest.mark.asyncio
async def test_keys_are_fetched_soon_after_boot(
    idp: FakeIdP, client: OIDCClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "monotonic", lambda: 5.0)
    token, nonce = await _id_token(idp, client)
    assert (await client.validate(token, nonce))["email"] == "person@example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("knob", "message"),
    [
        ({"audience": "someone-else"}, "another client"),
        ({"nonce": "not-this-one"}, "nonce"),
        ({"lifetime_seconds": -3600}, "expired"),
        ({"sign_with_unknown_key": True}, "signature"),
        ({"claims": {"iss": "https://evil.test"}}, "another issuer"),
    ],
)
async def test_bad_tokens_are_refused(
    idp: FakeIdP, client: OIDCClient, knob: dict[str, Any], message: str
) -> None:
    for name, value in knob.items():
        setattr(idp, name, value)
    token, nonce = await _id_token(idp, client)
    with pytest.raises(SignInError, match=message):
        await client.validate(token, nonce)


@pytest.mark.asyncio
async def test_symmetric_and_unsigned_tokens_are_refused(client: OIDCClient) -> None:
    now = int(time.time())
    claims = {
        "iss": "https://idp.test",
        "aud": "checkcord-test",
        "sub": "x",
        "iat": now,
        "exp": now + 60,
        "nonce": "n",
    }
    hs = jwt.encode(claims, "shared-" + "x" * 32, algorithm="HS256")
    with pytest.raises(SignInError, match="refused algorithm"):
        await client.validate(hs, "n")
    unsigned = jwt.encode(claims, None, algorithm="none")
    with pytest.raises(SignInError, match="refused algorithm"):
        await client.validate(unsigned, "n")


@pytest.mark.asyncio
async def test_a_second_audience_needs_azp(idp: FakeIdP, client: OIDCClient) -> None:
    idp.claims = {"aud": ["checkcord-test", "other"]}
    token, nonce = await _id_token(idp, client)
    with pytest.raises(SignInError, match="another client"):
        await client.validate(token, nonce)
    idp.claims = {"aud": ["checkcord-test", "other"], "azp": "checkcord-test"}
    token, nonce = await _id_token(idp, client)
    assert (await client.validate(token, nonce))["azp"] == "checkcord-test"


@pytest.mark.asyncio
async def test_discovery_must_name_the_configured_issuer(idp: FakeIdP) -> None:
    idp.issuer = "https://other.test"
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=idp.app))
    oidc = OIDCClient(_settings(), http=http)
    try:
        with pytest.raises(SignInError, match="different issuer"):
            await oidc.metadata()
    finally:
        await oidc.aclose()


@pytest.mark.asyncio
async def test_confidential_client_uses_basic_auth() -> None:
    idp = FakeIdP(client_secret="s-" + "y" * 20)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=idp.app))
    oidc = OIDCClient(_settings(oidc_client_secret=idp.client_secret), http=http)
    try:
        token, nonce = await _id_token(idp, oidc)
        assert (await oidc.validate(token, nonce))["sub"]
        oidc.client_secret = "wrong"
        with pytest.raises(SignInError, match="invalid_client"):
            await _id_token(idp, oidc)
    finally:
        await oidc.aclose()


@pytest.mark.asyncio
async def test_wrong_pkce_verifier_is_refused(idp: FakeIdP, client: OIDCClient) -> None:
    meta = await client.metadata()
    _, challenge = pkce_pair()
    url = client.authorization_url(meta, REDIRECT, "s", "n", challenge)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=idp.app)) as http:
        res = await http.get(url)
    code = parse_qs(urlsplit(res.headers["location"]).query)["code"][0]
    with pytest.raises(SignInError, match="invalid_grant"):
        await client.exchange(code, "some-other-verifier", REDIRECT)


@pytest.mark.parametrize(
    ("claims", "lenient", "expected"),
    [
        ({"email": "a@x.example", "email_verified": True}, False, "a@x.example"),
        ({"email": "a@x.example", "email_verified": "true"}, False, "a@x.example"),
        ({"email": "a@x.example", "email_verified": False}, True, None),
        ({"email": "a@x.example"}, False, None),
        ({"email": "a@x.example"}, True, "a@x.example"),
        ({"email": "not-an-address", "email_verified": True}, False, None),
        ({}, True, None),
    ],
)
def test_email_needs_verification(
    claims: dict[str, Any], lenient: bool, expected: str | None
) -> None:
    assert email_from_claims(claims, lenient) == expected


def test_groups_claim_accepts_a_list_or_a_string() -> None:
    assert groups_from_claims({"groups": ["a", "b", 3]}, "groups") == {"a", "b"}
    assert groups_from_claims({"roles": "checkcord-admins"}, "roles") == {"checkcord-admins"}
    assert groups_from_claims({}, "groups") == set()
