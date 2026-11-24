from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from checkcord import main
from checkcord.core import startup
from checkcord.core.config import PROVIDER_ADDRESSES, Settings, get_settings
from checkcord.core.lifecycle import AppContext
from checkcord.core.startup import StartupConfigError, validate_security_settings

VALID_KEY = Fernet.generate_key().decode()
PRIVATE_DB_URL = "postgresql+asyncpg://checkcord_app:s3cret-pw@db:5432/checkcord"


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_refuses_to_start_without_fernet_key(environment: str) -> None:
    with pytest.raises(StartupConfigError, match="FERNET_MASTER_KEY is not set"):
        validate_security_settings(Settings(environment=environment, fernet_master_key=None))


def test_refuses_to_start_with_invalid_fernet_key() -> None:
    settings = Settings(environment="production", fernet_master_key="not-a-fernet-key")
    with pytest.raises(StartupConfigError, match="FERNET_MASTER_KEY is not a valid Fernet key"):
        validate_security_settings(settings)


def test_starts_with_valid_fernet_key() -> None:
    validate_security_settings(
        Settings(environment="production", fernet_master_key=VALID_KEY, database_url=PRIVATE_DB_URL)
    )


@pytest.mark.parametrize("password", ["checkcord", "checkcord_app", "checkcord_owner"])
def test_production_refuses_shipped_database_password(password: str) -> None:
    settings = Settings(
        environment="production",
        fernet_master_key=VALID_KEY,
        database_url=f"postgresql+asyncpg://checkcord_app:{password}@db:5432/checkcord",
    )
    with pytest.raises(StartupConfigError, match="DATABASE_URL uses the shipped default password"):
        validate_security_settings(settings)


def test_production_refuses_shipped_migration_password() -> None:
    settings = Settings(
        environment="production",
        fernet_master_key=VALID_KEY,
        database_url=PRIVATE_DB_URL,
        migration_database_url="postgresql+asyncpg://checkcord_owner:checkcord_owner@db:5432/checkcord",
    )
    with pytest.raises(StartupConfigError, match="MIGRATION_DATABASE_URL"):
        validate_security_settings(settings)


@pytest.mark.parametrize("environment", ["development", "staging"])
def test_shipped_database_password_allowed_outside_production(
    environment: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    logger = MagicMock()
    monkeypatch.setattr(startup, "logger", logger)
    settings = Settings(
        environment=environment,
        fernet_master_key=VALID_KEY,
        database_url="postgresql+asyncpg://checkcord:checkcord@db:5432/checkcord",
    )
    validate_security_settings(settings)
    logger.warning.assert_not_called()


def test_development_logs_one_warning_instead(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = MagicMock()
    monkeypatch.setattr(startup, "logger", logger)

    validate_security_settings(Settings(environment="development", fernet_master_key=None))

    logger.warning.assert_called_once()


def test_test_environment_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = MagicMock()
    monkeypatch.setattr(startup, "logger", logger)

    validate_security_settings(Settings(environment="test", fernet_master_key=None))

    logger.warning.assert_not_called()


def test_api_refuses_to_start_in_production_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("FERNET_MASTER_KEY", raising=False)
    get_settings.cache_clear()
    entered = MagicMock()

    @asynccontextmanager
    async def lifecycle() -> AsyncGenerator[AppContext, None]:
        entered()
        yield MagicMock(spec=AppContext)

    with patch.object(main, "app_lifecycle", lifecycle):
        with pytest.raises(StartupConfigError, match="FERNET_MASTER_KEY"):
            with TestClient(main.app):
                pass

    entered.assert_not_called()


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


@pytest.mark.parametrize("name", PROVIDER_ADDRESSES)
def test_production_refuses_a_provider_address_without_https(name: str) -> None:
    settings = Settings(
        environment="production",
        fernet_master_key=VALID_KEY,
        database_url=PRIVATE_DB_URL,
        **{name: "http://provider.internal"},
    )
    with pytest.raises(StartupConfigError, match=f"{name.upper()} must use https in production"):
        validate_security_settings(settings)


def test_development_allows_a_plain_http_provider_address() -> None:
    settings = Settings(environment="development", github_api_url="http://localhost:9")
    assert startup.provider_address_problems(settings) == []
