import pytest
from fastapi.testclient import TestClient

from checkcord.core.config import Settings, get_settings
from checkcord.main import create_app


def test_cors_defaults_to_web_base_url() -> None:
    settings = Settings(web_base_url="https://checkcord.example.com")
    assert settings.cors_allowed_origins == ["https://checkcord.example.com"]


def test_cors_accepts_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://a.example.com, https://b.example.com")
    assert Settings().cors_allowed_origins == ["https://a.example.com", "https://b.example.com"]


def test_cors_accepts_json_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", '["https://a.example.com"]')
    assert Settings().cors_allowed_origins == ["https://a.example.com"]


def _preflight(client: TestClient, origin: str) -> dict[str, str]:
    resp = client.options(
        "/healthz",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )
    return dict(resp.headers)


def test_app_only_allows_configured_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://checkcord.example.com")
    get_settings.cache_clear()
    client = TestClient(create_app())

    allowed = _preflight(client, "https://checkcord.example.com")
    assert allowed["access-control-allow-origin"] == "https://checkcord.example.com"
    assert allowed["access-control-allow-credentials"] == "true"

    denied = _preflight(client, "https://evil.example.com")
    assert "access-control-allow-origin" not in denied
