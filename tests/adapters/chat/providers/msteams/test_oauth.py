import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, quote, urlsplit

import fakeredis
import pytest
from fastapi.testclient import TestClient
from rls_test_helpers import data_statements, table_statements
from sqlalchemy.dialects import postgresql

from checkcord.core.lifecycle import AppContext
from checkcord.core.oauth_state import mint_state
from checkcord.main import app

TENANT = uuid.UUID("11111111-2222-3333-4444-555555555555")


@pytest.fixture
def mock_ctx() -> AppContext:
    ctx = MagicMock(spec=AppContext)
    ctx.settings = MagicMock()
    ctx.settings.msteams_client_id = "ms-client-123"
    ctx.settings.msteams_redirect_uri = "https://test/msteams/redirect"
    ctx.settings.default_tenant_id = None
    ctx.settings.ms_login_url = "https://login.microsoftonline.com"

    ctx.db_engine = MagicMock()
    ctx.redis_client = None
    ctx.connection = MagicMock()
    ctx.connection.execute = AsyncMock(return_value=MagicMock())
    ctx.connection.scalar = AsyncMock(return_value=None)
    ctx.db_engine.begin.return_value.__aenter__ = AsyncMock(return_value=ctx.connection)
    ctx.db_engine.begin.return_value.__aexit__ = AsyncMock(return_value=None)

    return ctx


@pytest.fixture
def client(mock_ctx: AppContext) -> Iterator[TestClient]:
    @asynccontextmanager
    async def mock_lifecycle() -> AsyncGenerator[AppContext, None]:
        yield mock_ctx

    with patch("checkcord.main.app_lifecycle", mock_lifecycle), TestClient(app) as c:
        # Set after startup so the lifespan does not start stream consumers on it.
        mock_ctx.redis_client = fakeredis.aioredis.FakeRedis()
        yield c


def _q(state: str) -> str:
    return quote(state, safe="")


def _inserted(connection: MagicMock) -> dict[str, Any]:
    stmt = table_statements(connection, "msteams_installations")[-1]
    params: dict[str, Any] = stmt.compile(dialect=postgresql.dialect()).params
    return params


def test_msteams_install_redirect(client: TestClient) -> None:
    state = mint_state("teams", TENANT)
    resp = client.get(f"/msteams/install?state={_q(state)}", follow_redirects=False)
    assert resp.status_code == 307
    location = urlsplit(resp.headers["location"])
    assert location.geturl().startswith("https://login.microsoftonline.com/common/adminconsent")
    query = parse_qs(location.query)
    assert query["client_id"] == ["ms-client-123"]
    assert query["redirect_uri"] == ["https://test/msteams/redirect"]
    assert query["state"] == [state]


@pytest.mark.parametrize(
    "state",
    [
        None,
        "xyz",
        "tampered",
        "expired",
        "other-provider",
    ],
)
def test_msteams_install_rejects_bad_state(client: TestClient, state: str | None) -> None:
    if state == "tampered":
        good = mint_state("teams", TENANT)
        state = good[:-2] + ("AA" if not good.endswith("AA") else "BB")
    elif state == "expired":
        state = mint_state("teams", TENANT, ttl=-1)
    elif state == "other-provider":
        state = mint_state("slack", TENANT)
    url = "/msteams/install" + (f"?state={_q(state)}" if state else "")
    resp = client.get(url, follow_redirects=False)
    assert resp.status_code == 400


def test_msteams_install_missing_config(client: TestClient, mock_ctx: AppContext) -> None:
    mock_ctx.settings.msteams_client_id = None
    resp = client.get(f"/msteams/install?state={_q(mint_state('teams', TENANT))}")
    assert resp.status_code == 500


def test_msteams_redirect_missing_tenant(client: TestClient) -> None:
    resp = client.get(f"/msteams/oauth_redirect?state={_q(mint_state('teams', TENANT))}")
    assert resp.status_code == 400


@pytest.mark.parametrize("bad", ["missing", "unsigned", "tampered", "expired"])
def test_msteams_redirect_rejects_bad_state(
    client: TestClient, mock_ctx: AppContext, bad: str
) -> None:
    state = {
        "missing": None,
        "unsigned": str(TENANT),
        "tampered": mint_state("teams", TENANT)[:-3] + "abc",
        "expired": mint_state("teams", TENANT, ttl=-1),
    }[bad]
    url = "/msteams/oauth_redirect?tenant=ms-tenant-456" + (f"&state={_q(state)}" if state else "")
    resp = client.get(url)
    assert resp.status_code == 400
    mock_ctx.db_engine.begin.assert_not_called()


def test_msteams_redirect_binds_state_tenant(client: TestClient, mock_ctx: AppContext) -> None:
    resp = client.get(
        f"/msteams/oauth_redirect?tenant=ms-tenant-456&state={_q(mint_state('teams', TENANT))}"
    )
    assert resp.status_code == 200
    assert "Microsoft Teams is connected to Checkcord. You can close this tab." in resp.text
    params = _inserted(mock_ctx.connection)
    assert params["tenant_id"] == TENANT
    assert params["ms_tenant_id"] == "ms-tenant-456"
    assert "state" not in params["auth_payload"]


def test_msteams_redirect_state_is_single_use(client: TestClient) -> None:
    url = f"/msteams/oauth_redirect?tenant=ms-tenant-456&state={_q(mint_state('teams', TENANT))}"
    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 400


def test_msteams_redirect_refuses_other_tenants_install(
    client: TestClient, mock_ctx: AppContext
) -> None:
    mock_ctx.connection.scalar = AsyncMock(return_value=uuid.uuid4())
    resp = client.get(
        f"/msteams/oauth_redirect?tenant=ms-tenant-456&state={_q(mint_state('teams', TENANT))}"
    )
    assert resp.status_code == 409
    assert data_statements(mock_ctx.connection) == []


def test_msteams_redirect_same_tenant_reinstall_updates(
    client: TestClient, mock_ctx: AppContext
) -> None:
    mock_ctx.connection.scalar = AsyncMock(return_value=TENANT)
    resp = client.get(
        f"/msteams/oauth_redirect?tenant=ms-tenant-456&state={_q(mint_state('teams', TENANT))}"
    )
    assert resp.status_code == 200
    assert _inserted(mock_ctx.connection)["tenant_id"] == TENANT


def test_msteams_redirect_consent_error(client: TestClient, mock_ctx: AppContext) -> None:
    resp = client.get(
        f"/msteams/oauth_redirect?error=access_denied&state={_q(mint_state('teams', TENANT))}"
    )
    assert resp.status_code == 400
    mock_ctx.db_engine.begin.assert_not_called()


def test_msteams_install_redirect_uses_the_configured_login_address(
    client: TestClient, mock_ctx: AppContext
) -> None:
    mock_ctx.settings.ms_login_url = "https://login.microsoftonline.us"
    state = mint_state("teams", TENANT)
    resp = client.get(f"/msteams/install?state={_q(state)}", follow_redirects=False)
    assert resp.headers["location"].startswith(
        "https://login.microsoftonline.us/common/adminconsent?"
    )
