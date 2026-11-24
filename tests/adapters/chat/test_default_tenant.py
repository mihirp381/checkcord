import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import fakeredis
import pytest
from fastapi.testclient import TestClient
from rls_test_helpers import data_statements
from slack_sdk.oauth.installation_store import Bot, Installation
from sqlalchemy.dialects import postgresql

from checkcord.adapters.chat.providers.slack.oauth import PostgresInstallationStore
from checkcord.core.config import Settings
from checkcord.core.lifecycle import AppContext
from checkcord.core.oauth_state import mint_state
from checkcord.main import app

DEFAULT_TENANT = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _mock_engine(existing_tenant: uuid.UUID | None) -> tuple[MagicMock, MagicMock]:
    engine = MagicMock()
    connection = MagicMock()
    engine.begin.return_value.__aenter__ = AsyncMock(return_value=connection)
    engine.begin.return_value.__aexit__ = AsyncMock(return_value=None)
    result = MagicMock()
    result.fetchone.return_value = (existing_tenant,) if existing_tenant else None
    connection.execute = AsyncMock(return_value=result)
    return engine, connection


def _inserted_tenant(connection: MagicMock) -> Any:
    insert_stmt = data_statements(connection)[-1]
    return insert_stmt.compile(dialect=postgresql.dialect()).params["tenant_id"]


def _installation() -> Installation:
    return Installation(app_id="A1", team_id="T1", bot_token="xoxb-1", user_id="U1")


def _bot() -> Bot:
    return Bot(
        app_id="A1",
        team_id="T1",
        bot_token="xoxb-1",
        bot_id="B1",
        bot_user_id="U1",
        installed_at=1.0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["installation", "bot"])
async def test_slack_new_install_joins_default_tenant(method: str) -> None:
    engine, connection = _mock_engine(existing_tenant=None)
    store = PostgresInstallationStore(engine, Settings(default_tenant_id=DEFAULT_TENANT))

    if method == "installation":
        await store.async_save(_installation())
    else:
        await store.async_save_bot(_bot())

    assert _inserted_tenant(connection) == DEFAULT_TENANT


@pytest.mark.asyncio
async def test_slack_new_install_without_default_gets_fresh_tenant() -> None:
    engine, connection = _mock_engine(existing_tenant=None)
    store = PostgresInstallationStore(engine, Settings(default_tenant_id=None))

    await store.async_save(_installation())

    tenant = _inserted_tenant(connection)
    assert isinstance(tenant, uuid.UUID)
    assert tenant != DEFAULT_TENANT


@pytest.mark.asyncio
async def test_slack_existing_install_keeps_its_tenant() -> None:
    existing = uuid.uuid4()
    engine, connection = _mock_engine(existing_tenant=existing)
    store = PostgresInstallationStore(engine, Settings(default_tenant_id=DEFAULT_TENANT))

    await store.async_save(_installation())

    assert _inserted_tenant(connection) == existing


def test_settings_parse_default_tenant_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEFAULT_TENANT_ID", str(DEFAULT_TENANT))
    assert Settings().default_tenant_id == DEFAULT_TENANT


def _teams_redirect(
    default_tenant: uuid.UUID | None, existing: uuid.UUID | None, state_tenant: uuid.UUID
) -> tuple[int, Any]:
    ctx = MagicMock(spec=AppContext)
    ctx.settings = Settings(default_tenant_id=default_tenant)
    ctx.redis_client = None
    ctx.db_engine, connection = _mock_engine(existing_tenant=existing)
    connection.scalar = AsyncMock(return_value=existing)

    @asynccontextmanager
    async def lifecycle() -> AsyncGenerator[AppContext, None]:
        yield ctx

    state = quote(mint_state("teams", state_tenant), safe="")
    with patch("checkcord.main.app_lifecycle", lifecycle), TestClient(app) as client:
        ctx.redis_client = fakeredis.aioredis.FakeRedis()
        resp = client.get(f"/msteams/oauth_redirect?tenant=ms-tenant-1&state={state}")
    if resp.status_code != 200:
        return resp.status_code, None
    return resp.status_code, _inserted_tenant(connection)


def test_teams_new_install_binds_link_tenant_not_default() -> None:
    link_tenant = uuid.uuid4()
    assert _teams_redirect(DEFAULT_TENANT, None, link_tenant) == (200, link_tenant)


def test_teams_reinstall_by_owner_keeps_its_tenant() -> None:
    existing = uuid.uuid4()
    assert _teams_redirect(DEFAULT_TENANT, existing, existing) == (200, existing)


def test_teams_install_cannot_take_over_other_tenant() -> None:
    assert _teams_redirect(DEFAULT_TENANT, uuid.uuid4(), DEFAULT_TENANT) == (409, None)
