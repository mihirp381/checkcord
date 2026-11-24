import pytest

from checkcord.core.config import Settings

pytestmark = pytest.mark.integration


def test_settings_load_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that settings load with default values."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    settings = Settings()
    assert settings.environment == "development"
    assert "postgresql+asyncpg" in settings.database_url


def test_settings_from_fixture(test_settings: Settings) -> None:
    """Test that fixture settings override defaults correctly."""
    assert test_settings.environment == "test"
    assert "asyncpg" in test_settings.database_url
    assert "redis://" in test_settings.redis_url


def test_settings_embedding_defaults_post_cutover() -> None:
    """Defaults are voyage-code-3 at 1024 dimensions."""
    settings = Settings()
    assert settings.embedding_provider == "voyage"
    assert settings.embedding_model == "voyage-code-3"
    assert settings.embedding_dimension == 1024


def test_settings_embedding_provider_openai_still_supported() -> None:
    """Setting the openai provider explicitly is accepted and not coerced."""
    settings = Settings(embedding_provider="openai", embedding_model="text-embedding-3-small")
    assert settings.embedding_provider == "openai"
    assert settings.embedding_model == "text-embedding-3-small"


def test_settled_model_follows_the_extraction_model() -> None:
    assert Settings(chat_extraction_model="local-7b").chat_settled_model == "local-7b"
    assert (
        Settings(chat_extraction_model="local-7b", chat_settled_model="judge").chat_settled_model
        == "judge"
    )
