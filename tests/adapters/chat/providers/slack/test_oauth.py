import html
import re
import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from urllib.parse import quote

import fakeredis
import pytest
from fastapi.testclient import TestClient
from rls_test_helpers import data_statements, table_statements
from slack_bolt.error import BoltError
from slack_bolt.oauth.async_oauth_settings import AsyncOAuthSettings
from slack_bolt.request.async_request import AsyncBoltRequest
from slack_bolt.response import BoltResponse
from slack_sdk.oauth.installation_store import Bot, Installation
from sqlalchemy.dialects import postgresql

from checkcord.adapters.chat.providers.slack.oauth import (
    CheckcordOAuthFlow,
    PostgresInstallationStore,
    SignedStateStore,
    open_installation_info,
    seal_installation_info,
)
from checkcord.core.crypto import decrypt_secret
from checkcord.core.lifecycle import AppContext
from checkcord.core.oauth_state import mint_state
from checkcord.main import app


@pytest.fixture
def mock_engine() -> tuple[MagicMock, MagicMock]:
    engine = MagicMock()
    connection = MagicMock()
    engine.begin.return_value.__aenter__ = AsyncMock(return_value=connection)
    engine.begin.return_value.__aexit__ = AsyncMock(return_value=None)
    engine.connect.return_value.__aenter__ = AsyncMock(return_value=connection)
    engine.connect.return_value.__aexit__ = AsyncMock(return_value=None)
    return engine, connection


@pytest.mark.asyncio
async def test_postgres_installation_store_save_new_tenant(
    mock_engine: tuple[MagicMock, MagicMock],
) -> None:
    engine, connection = mock_engine

    # Mock no existing team
    mock_result = MagicMock()
    mock_result.fetchone.return_value = None
    connection.execute = AsyncMock(return_value=mock_result)

    store = PostgresInstallationStore(engine)
    inst = Installation(
        app_id="A123",
        enterprise_id="E123",
        team_id="T123",
        bot_token="xoxb-bot",
        bot_id="B123",
        bot_user_id="U123",
        user_id="U_USR",
        installed_at=12345.67,
    )

    with patch("checkcord.adapters.chat.providers.slack.oauth.logger") as mock_logger:
        await store.async_save(inst)
        # Should generate a new tenant_id and log "tenant_auto_provisioned"
        mock_logger.info.assert_any_call(
            "tenant_auto_provisioned",
            team_id="T123",
            tenant_id=ANY,
            platform="slack",
        )

    # Check connection.execute was called
    assert len(data_statements(connection)) == 2  # select, then insert


@pytest.mark.asyncio
async def test_postgres_installation_store_save_existing_tenant(
    mock_engine: tuple[MagicMock, MagicMock],
) -> None:
    engine, connection = mock_engine

    # Mock existing team with tenant_id
    existing_tenant_id = uuid.uuid4()
    mock_result = MagicMock()
    mock_result.fetchone.return_value = (existing_tenant_id,)
    connection.execute = AsyncMock(return_value=mock_result)

    store = PostgresInstallationStore(engine)
    inst = Installation(
        app_id="A123",
        enterprise_id="E123",
        team_id="T123",
        bot_token="xoxb-bot",
        bot_id="B123",
        bot_user_id="U123",
        user_id="U_USR",
        installed_at=12345.67,
    )

    with patch("checkcord.adapters.chat.providers.slack.oauth.logger") as mock_logger:
        await store.async_save(inst)
        # Should reuse existing tenant_id and NOT log "tenant_auto_provisioned"
        for call in mock_logger.info.call_args_list:
            assert "tenant_auto_provisioned" not in call[0]

    assert len(data_statements(connection)) == 2


@pytest.mark.asyncio
async def test_postgres_installation_store_save_bot(
    mock_engine: tuple[MagicMock, MagicMock],
) -> None:
    engine, connection = mock_engine

    # Mock no existing team
    mock_result = MagicMock()
    mock_result.fetchone.return_value = None
    connection.execute = AsyncMock(return_value=mock_result)

    store = PostgresInstallationStore(engine)
    bot = Bot(
        app_id="A123",
        enterprise_id="E123",
        team_id="T123",
        bot_token="xoxb-bot",
        bot_id="B123",
        bot_user_id="U123",
        installed_at=12345.67,
    )

    with patch("checkcord.adapters.chat.providers.slack.oauth.logger") as mock_logger:
        await store.async_save_bot(bot)
        mock_logger.info.assert_any_call(
            "tenant_auto_provisioned",
            team_id="T123",
            tenant_id=ANY,
            platform="slack",
        )

    assert len(data_statements(connection)) == 2


@pytest.mark.asyncio
async def test_postgres_installation_store_find_bot(
    mock_engine: tuple[MagicMock, MagicMock],
) -> None:
    engine, connection = mock_engine

    # Mock found info
    bot_info = {
        "app_id": "A123",
        "enterprise_id": "E123",
        "team_id": "T123",
        "bot_token": "xoxb-bot",
        "bot_id": "B123",
        "bot_user_id": "U123",
        "installed_at": 12345.67,
    }
    mock_result = MagicMock()
    mock_result.fetchone.return_value = (bot_info,)
    connection.execute = AsyncMock(return_value=mock_result)

    store = PostgresInstallationStore(engine)
    bot = await store.async_find_bot(enterprise_id="E123", team_id="T123")
    assert bot is not None
    assert bot.bot_token == "xoxb-bot"
    assert bot.bot_id == "B123"


@pytest.mark.asyncio
async def test_postgres_installation_store_find_installation(
    mock_engine: tuple[MagicMock, MagicMock],
) -> None:
    engine, connection = mock_engine

    # Mock found info
    inst_info = {
        "app_id": "A123",
        "enterprise_id": "E123",
        "team_id": "T123",
        "bot_token": "xoxb-bot",
        "bot_id": "B123",
        "bot_user_id": "U123",
        "user_id": "U_USR",
        "installed_at": 12345.67,
    }
    mock_result = MagicMock()
    mock_result.fetchone.return_value = (inst_info,)
    connection.execute = AsyncMock(return_value=mock_result)

    store = PostgresInstallationStore(engine)
    inst = await store.async_find_installation(enterprise_id="E123", team_id="T123")
    assert inst is not None
    assert inst.bot_token == "xoxb-bot"
    assert inst.user_id == "U_USR"


TENANT = uuid.UUID("11111111-2222-3333-4444-555555555555")


@pytest.fixture
def slack_client() -> Iterator[tuple[TestClient, MagicMock]]:
    mock_ctx = MagicMock(spec=AppContext)
    mock_ctx.settings = MagicMock()
    mock_ctx.settings.slack_client_id = "test-id"
    mock_ctx.settings.slack_client_secret = "test-secret"
    mock_ctx.settings.slack_redirect_uri = "http://test/redirect"
    mock_ctx.db_engine = MagicMock()
    mock_ctx.redis_client = None

    @asynccontextmanager
    async def mock_lifecycle() -> AsyncGenerator[AppContext, None]:
        yield mock_ctx

    mock_flow = MagicMock()
    mock_flow.handle_installation = AsyncMock(
        return_value=BoltResponse(status=200, body="Install page")
    )
    mock_flow.handle_callback = AsyncMock(
        return_value=BoltResponse(status=200, body="Callback success")
    )

    with (
        patch("checkcord.main.app_lifecycle", mock_lifecycle),
        patch(
            "checkcord.adapters.chat.providers.slack.oauth.get_oauth_flow",
            return_value=mock_flow,
        ),
        TestClient(app) as client,
    ):
        yield client, mock_flow


def test_slack_endpoints(slack_client: tuple[TestClient, MagicMock]) -> None:
    client, flow = slack_client
    state = quote(mint_state("slack", TENANT), safe="")
    resp = client.get(f"/slack/install?state={state}")
    assert resp.status_code == 200
    assert resp.text == "Install page"

    resp2 = client.get(f"/slack/oauth_redirect?code=123&state={state}")
    assert resp2.status_code == 200
    assert "Slack is connected to Checkcord. You can close this tab." in resp2.text


@pytest.mark.parametrize("path", ["/slack/install", "/slack/oauth_redirect?code=123"])
@pytest.mark.parametrize("bad", ["missing", "unsigned", "tampered", "expired", "teams"])
def test_slack_endpoints_reject_bad_state(
    slack_client: tuple[TestClient, MagicMock], path: str, bad: str
) -> None:
    client, flow = slack_client
    good = mint_state("slack", TENANT)
    state = {
        "missing": None,
        "unsigned": "plain-bolt-state",
        "tampered": good[:-3] + ("abc" if not good.endswith("abc") else "xyz"),
        "expired": mint_state("slack", TENANT, ttl=-1),
        "teams": mint_state("teams", TENANT),
    }[bad]
    sep = "&" if "?" in path else "?"
    url = path + (f"{sep}state={quote(state, safe='')}" if state else "")
    assert client.get(url).status_code == 400
    flow.handle_installation.assert_not_awaited()
    flow.handle_callback.assert_not_awaited()


def _flow(store: Any, redis: Any) -> CheckcordOAuthFlow:
    return CheckcordOAuthFlow(
        settings=AsyncOAuthSettings(
            client_id="test-id",
            client_secret="test-secret",
            redirect_uri="http://test/redirect",
            installation_store=store,
            state_store=SignedStateStore(redis),
            state_expiration_seconds=900,
            scopes=["commands"],
        )
    )


def _bolt(state: str, cookie: str | None = None) -> AsyncBoltRequest:
    headers = {"cookie": f"slack-app-oauth-state={cookie}"} if cookie else {}
    return AsyncBoltRequest(body="", query={"state": state, "code": "c0de"}, headers=headers)


@pytest.mark.asyncio
async def test_bolt_flow_install_page_carries_our_state() -> None:
    flow = _flow(MagicMock(spec=PostgresInstallationStore), fakeredis.aioredis.FakeRedis())
    state = mint_state("slack", TENANT)
    resp = await flow.handle_installation(_bolt(state))
    assert resp.status == 200
    assert quote(state, safe="") in resp.body or state in resp.body
    assert any(f"slack-app-oauth-state={state}" in c for c in resp.headers["set-cookie"])


@pytest.mark.asyncio
async def test_bolt_callback_saves_for_state_tenant_once() -> None:
    store = MagicMock(spec=PostgresInstallationStore)
    store.save_for_tenant = AsyncMock()
    flow = _flow(store, fakeredis.aioredis.FakeRedis())
    installation = Installation(app_id="A1", team_id="T1", bot_token="xoxb-1", user_id="U1")
    flow.run_installation = AsyncMock(return_value=installation)  # type: ignore[method-assign]
    state = mint_state("slack", TENANT)

    first = await flow.handle_callback(_bolt(state, cookie=state))
    assert first.status == 200, first.body
    store.save_for_tenant.assert_awaited_once_with(installation, TENANT)

    replay = await flow.handle_callback(_bolt(state, cookie=state))
    assert replay.status != 200
    assert store.save_for_tenant.await_count == 1


@pytest.mark.asyncio
async def test_bolt_callback_requires_matching_browser_cookie() -> None:
    store = MagicMock(spec=PostgresInstallationStore)
    store.save_for_tenant = AsyncMock()
    flow = _flow(store, fakeredis.aioredis.FakeRedis())
    flow.run_installation = AsyncMock()  # type: ignore[method-assign]
    resp = await flow.handle_callback(_bolt(mint_state("slack", TENANT)))
    assert resp.status != 200
    flow.run_installation.assert_not_awaited()
    store.save_for_tenant.assert_not_awaited()


@pytest.mark.asyncio
async def test_signed_state_store_never_issues() -> None:
    with pytest.raises(BoltError):
        await SignedStateStore(fakeredis.aioredis.FakeRedis()).async_issue()


@pytest.mark.asyncio
async def test_save_for_tenant_encrypts_and_binds(
    mock_engine: tuple[MagicMock, MagicMock],
) -> None:
    engine, connection = mock_engine
    connection.scalar = AsyncMock(return_value=None)
    connection.execute = AsyncMock()
    inst = Installation(
        app_id="A1", team_id="T1", bot_token="xoxb-1", user_token="xoxp-1", user_id="U1"
    )
    await PostgresInstallationStore(engine).save_for_tenant(inst, TENANT)
    params = (
        table_statements(connection, "slack_installations")[-1]
        .compile(dialect=postgresql.dialect())
        .params
    )
    assert params["tenant_id"] == TENANT
    assert params["bot_token"] != "xoxb-1"
    assert decrypt_secret(params["bot_token"]) == "xoxb-1"
    info = params["installation_info"]
    assert "xoxb-1" not in str(info) and "xoxp-1" not in str(info)
    assert open_installation_info(info)["user_token"] == "xoxp-1"


@pytest.mark.asyncio
async def test_save_for_tenant_refuses_other_tenants_team(
    mock_engine: tuple[MagicMock, MagicMock],
) -> None:
    engine, connection = mock_engine
    connection.scalar = AsyncMock(return_value=uuid.uuid4())
    connection.execute = AsyncMock()
    inst = Installation(app_id="A1", team_id="T1", bot_token="xoxb-1", user_id="U1")
    with pytest.raises(BoltError):
        await PostgresInstallationStore(engine).save_for_tenant(inst, TENANT)
    assert data_statements(connection) == []


@pytest.mark.asyncio
async def test_find_bot_decrypts_sealed_tokens(
    mock_engine: tuple[MagicMock, MagicMock],
) -> None:
    engine, connection = mock_engine
    info = seal_installation_info(
        {
            "app_id": "A1",
            "team_id": "T1",
            "bot_token": "xoxb-9",
            "bot_id": "B1",
            "bot_user_id": "U1",
            "installed_at": 1.0,
        }
    )
    result = MagicMock()
    result.fetchone.return_value = (info,)
    connection.execute = AsyncMock(return_value=result)
    bot = await PostgresInstallationStore(engine).async_find_bot(enterprise_id=None, team_id="T1")
    assert bot is not None and bot.bot_token == "xoxb-9"


@pytest.mark.asyncio
async def test_postgres_installation_store_delete(
    mock_engine: tuple[MagicMock, MagicMock],
) -> None:
    engine, connection = mock_engine
    connection.execute = AsyncMock()

    store = PostgresInstallationStore(engine)

    await store.async_delete_bot(enterprise_id="E123", team_id="T123")
    assert len(data_statements(connection)) == 1

    await store.async_delete_installation(enterprise_id="E123", team_id="T123")
    assert len(data_statements(connection)) == 2

    await store.async_delete_all(enterprise_id="E123", team_id="T123")
    assert len(data_statements(connection)) == 3


def _install_scopes(settings: Any) -> list[str]:
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlparse

    from fastapi import FastAPI

    from checkcord.adapters.chat.providers.slack.oauth import router

    local = FastAPI()
    local.include_router(router)
    local.state.ctx = SimpleNamespace(
        settings=settings, db_engine=MagicMock(), redis_client=fakeredis.aioredis.FakeRedis()
    )
    state = quote(mint_state("slack", TENANT), safe="")
    resp = TestClient(local).get(f"/slack/install?state={state}", follow_redirects=False)
    assert resp.status_code == 200, resp.text
    match = re.search(r"https://slack\.com/oauth/v2/authorize\?[^\"'\s<>]+", resp.text)
    assert match, resp.text
    query = parse_qs(urlparse(html.unescape(match.group(0))).query)
    return query["scope"][0].split(",")


def test_install_link_requests_default_bot_scopes() -> None:
    from checkcord.core.config import SLACK_BOT_SCOPES, Settings

    settings = Settings(
        slack_client_id="cid", slack_client_secret="secret", slack_redirect_uri="http://t/cb"
    )
    assert _install_scopes(settings) == list(SLACK_BOT_SCOPES)


@pytest.mark.parametrize("raw", ["chat:write, channels:read", '["chat:write","channels:read"]'])
def test_install_link_requests_configured_scopes(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    from checkcord.core.config import Settings

    monkeypatch.setenv("SLACK_BOT_SCOPES", raw)
    settings = Settings(
        slack_client_id="cid", slack_client_secret="secret", slack_redirect_uri="http://t/cb"
    )
    assert _install_scopes(settings) == ["chat:write", "channels:read"]
