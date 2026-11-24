from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from checkcord.core.config import Settings, get_settings
from checkcord.core.oidc import OIDCClient
from checkcord.core.startup import StartupConfigError, sign_in_problems, validate_security_settings
from checkcord.main import app, create_app
from checkcord.services.doctor import sign_in_check
from sim.world.idp import FakeIdP

NOT_CONFIGURED = "Sign-in is not configured on this server; use an API key."


@pytest.fixture
def no_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("OIDC_ISSUER", "OIDC_CLIENT_ID"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_without_a_provider_the_login_endpoints_say_so(no_provider: None) -> None:
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/api/v1/auth/config").json() == {
        "sign_in_enabled": False,
        "login_url": None,
    }
    for method, path in (
        ("GET", "/api/v1/auth/login"),
        ("GET", "/api/v1/auth/callback?code=c&state=s"),
    ):
        res = client.request(method, path, follow_redirects=False)
        assert res.status_code == 404 and res.json() == {"detail": NOT_CONFIGURED}


def test_with_a_provider_config_names_the_login_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OIDC_ISSUER", "https://idp.test")
    monkeypatch.setenv("OIDC_CLIENT_ID", "checkcord-test")
    get_settings.cache_clear()
    try:
        res = TestClient(app).get("/api/v1/auth/config").json()
    finally:
        get_settings.cache_clear()
    assert res == {"sign_in_enabled": True, "login_url": "/api/v1/auth/login"}


def _sign_in(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "production",
        "oidc_issuer": "https://idp.test",
        "oidc_client_id": "checkcord-test",
        "default_tenant_id": uuid4(),
        "public_base_url": "https://checkcord.example.com",
    }
    values.update(overrides)
    return Settings(**values)


def test_sign_in_problems() -> None:
    assert sign_in_problems(Settings(environment="production")) == []
    assert sign_in_problems(_sign_in()) == []
    assert _sign_in().effective_oidc_redirect_uri == (
        "https://checkcord.example.com/api/v1/auth/callback"
    )
    assert sign_in_problems(_sign_in(default_tenant_id=None)) == [
        "Sign-in needs DEFAULT_TENANT_ID: people sign in to that tenant."
    ]
    assert sign_in_problems(_sign_in(public_base_url=None)) == [
        "Sign-in needs OIDC_REDIRECT_URI or PUBLIC_BASE_URL."
    ]
    assert sign_in_problems(_sign_in(oidc_issuer="http://idp.test")) == [
        "OIDC_ISSUER must use https in production."
    ]
    assert sign_in_problems(_sign_in(environment="development", oidc_issuer="http://idp")) == []


def test_production_refuses_half_configured_sign_in() -> None:
    with pytest.raises(StartupConfigError, match="DEFAULT_TENANT_ID"):
        validate_security_settings(_sign_in(default_tenant_id=None))


@pytest.mark.asyncio
async def test_doctor_reports_sign_in() -> None:
    check = await sign_in_check(Settings(environment="test"))
    assert check.ok and check.detail == "not configured; API keys only"
    check = await sign_in_check(_sign_in(default_tenant_id=None))
    assert not check.ok and "DEFAULT_TENANT_ID" in check.detail

    idp = FakeIdP(issuer="https://idp.test", client_id="checkcord-test")
    settings = _sign_in()
    oidc = OIDCClient(settings, http=httpx.AsyncClient(transport=httpx.ASGITransport(app=idp.app)))
    try:
        check = await sign_in_check(settings, oidc)
        assert check.ok and "issuer https://idp.test reachable" in check.detail
        idp.issuer = "https://elsewhere.test"
        check = await sign_in_check(settings, oidc)
        assert not check.ok and "not usable" in check.detail
    finally:
        await oidc.aclose()


@pytest.mark.parametrize(
    ("environment", "flag", "served"),
    [
        ("development", None, True),
        ("production", None, False),
        ("production", "true", True),
        ("development", "false", False),
    ],
)
def test_api_docs_follow_the_setting(
    monkeypatch: pytest.MonkeyPatch, environment: str, flag: str | None, served: bool
) -> None:
    monkeypatch.setenv("ENVIRONMENT", environment)
    if flag is None:
        monkeypatch.delenv("API_DOCS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("API_DOCS_ENABLED", flag)
    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        for path in ("/docs", "/openapi.json"):
            assert (client.get(path).status_code == 200) is served, path
    finally:
        get_settings.cache_clear()
