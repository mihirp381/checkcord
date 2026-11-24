from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from checkcord.adapters.chat.providers.msteams import auth
from checkcord.adapters.chat.providers.slack import app as slack_app
from checkcord.adapters.chat.providers.slack import connector as slack_connector
from checkcord.adapters.code.providers.github import poller as github_poller
from checkcord.adapters.code.providers.github.rest_client import GitHubRestClient
from checkcord.adapters.notion_adapter import NotionAdapter
from checkcord.adapters.tasks.providers.linear import router as linear
from checkcord.adapters.tasks.providers.notion import router as notion
from checkcord.core.config import PROVIDER_ADDRESSES, Settings, get_settings
from checkcord.core.oauth_state import mint_state
from checkcord.services import doctor

ADDRESSES = {
    "github_api_url": "https://ghe.example.com/api/v3",
    "slack_api_url": "https://slack.example.com/api/",
    "msgraph_url": "https://graph.microsoft.us",
    "ms_login_url": "https://login.microsoftonline.us",
    "botframework_openid_url": "https://login.botframework.azure.us/v1/.well-known/openidconfiguration",
    "linear_api_url": "https://linear-api.example.com",
    "linear_oauth_url": "https://linear.example.com",
    "notion_api_url": "https://notion.example.com",
}


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    for name, value in ADDRESSES.items():
        monkeypatch.setenv(name.upper(), value)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def _response(status: int = 200, body: Any = None) -> httpx.Response:
    return httpx.Response(
        status, json=body if body is not None else [], request=httpx.Request("GET", "x")
    )


def test_every_address_is_covered_here() -> None:
    assert set(ADDRESSES) == set(PROVIDER_ADDRESSES)


def test_addresses_drop_a_trailing_slash_and_slack_keeps_one() -> None:
    settings = Settings(
        environment="test",
        github_api_url="https://ghe.example.com/api/v3/",
        slack_api_url="https://s/api",
    )
    assert settings.github_api_url == "https://ghe.example.com/api/v3"
    assert settings.slack_api_url == "https://s/api/"


def test_doctor_lists_only_the_changed_addresses() -> None:
    assert doctor.provider_address_check(Settings(environment="test")).detail == "all defaults"
    check = doctor.provider_address_check(
        Settings(environment="test", github_api_url="http://host.docker.internal:9")
    )
    assert check.ok and check.warn
    assert check.detail == "not the default: GITHUB_API_URL=http://host.docker.internal:9"


def test_doctor_fails_a_plain_http_address_in_production() -> None:
    check = doctor.provider_address_check(
        Settings(environment="production", notion_api_url="http://notion.internal")
    )
    assert not check.ok
    assert check.detail == "NOTION_API_URL must use https in production."


@pytest.mark.asyncio
async def test_github_rest_client_calls_the_configured_base(configured: Settings) -> None:
    with patch("httpx.AsyncClient") as client_class:
        http = AsyncMock()
        http.get = AsyncMock(return_value=MagicMock(status_code=200, content=b"x"))
        http.__aenter__ = AsyncMock(return_value=http)
        http.__aexit__ = AsyncMock(return_value=None)
        client_class.return_value = http
        await GitHubRestClient("pat").get_file_content("o", "r", "a.py", "main")
    assert http.get.call_args.args[0] == "https://ghe.example.com/api/v3/repos/o/r/contents/a.py"


@pytest.mark.asyncio
async def test_github_poller_calls_the_configured_base(configured: Settings) -> None:
    engine = MagicMock()
    conn = AsyncMock()
    result = MagicMock()
    result.mappings.return_value.fetchall.return_value = [
        {"repo_identifier": "o/r", "tenant_id": uuid4(), "personal_access_token": "pat"}
    ]
    conn.execute = AsyncMock(return_value=result)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    engine.connect.return_value = conn
    send = AsyncMock(return_value=_response())
    with (
        patch.object(github_poller.http_retry, "send", send),
        patch("checkcord.services.scope.allows", AsyncMock(return_value=True)),
    ):
        await github_poller.poll_github_history(engine, MagicMock(redis=None))
    assert send.await_args.args[3] == "https://ghe.example.com/api/v3/repos/o/r/events"


def test_slack_app_and_install_page_use_the_configured_base(configured: Settings) -> None:
    settings = configured.model_copy(update={"slack_client_id": "id", "slack_client_secret": "pw"})
    app = slack_app.init_slack_app(settings, MagicMock())
    assert app.client.base_url == "https://slack.example.com/api/"
    assert app.oauth_flow is not None
    generator = app.oauth_flow.settings.authorize_url_generator
    assert generator.authorization_url == "https://slack.example.com/oauth/v2/authorize"


@pytest.mark.asyncio
async def test_slack_revoke_calls_the_configured_base(configured: Settings) -> None:
    client = MagicMock()
    client.return_value.apps_uninstall = AsyncMock(return_value={"ok": True})
    with (
        patch("slack_sdk.web.async_client.AsyncWebClient", client),
        patch.object(slack_connector, "decrypt_secret", lambda v: v),
    ):
        await slack_connector._revoke({"bot_token": "xoxb-test"}, configured)
    assert client.call_args.kwargs["base_url"] == "https://slack.example.com/api/"


@pytest.mark.asyncio
async def test_bot_framework_keys_come_from_the_configured_metadata(configured: Settings) -> None:
    fetched: list[str] = []

    async def fetch(url: str) -> dict[str, Any]:
        fetched.append(url)
        return {
            "issuer": "https://api.botframework.us",
            "jwks_uri": "https://keys.example.com",
            "keys": [],
        }

    keys = auth.SigningKeys(fetch=fetch)
    await keys.get("k1")
    assert fetched[0] == ADDRESSES["botframework_openid_url"]
    assert keys.issuer == "https://api.botframework.us"


@pytest.mark.asyncio
async def test_linear_install_and_api_use_the_configured_addresses(configured: Settings) -> None:
    settings = configured.model_copy(
        update={"linear_client_id": "id", "linear_redirect_uri": "https://c/redirect"}
    )
    with patch.object(linear, "get_settings", return_value=settings):
        redirect = await linear.linear_install(state=mint_state("linear", uuid4()))
    assert redirect.headers["location"].startswith("https://linear.example.com/oauth/authorize?")
    assert linear._api("graphql") == "https://linear-api.example.com/graphql"


@pytest.mark.asyncio
async def test_notion_install_and_blocks_use_the_configured_address(configured: Settings) -> None:
    settings = configured.model_copy(
        update={"notion_client_id": "id", "notion_redirect_uri": "https://c/redirect"}
    )
    with patch.object(notion, "get_settings", return_value=settings):
        redirect = await notion.notion_install(state=mint_state("notion", uuid4()))
    assert redirect.headers["location"].startswith("https://notion.example.com/v1/oauth/authorize?")

    send = AsyncMock(return_value=_response(body={"results": [], "has_more": False}))
    with patch("checkcord.adapters.notion_adapter.http_retry.send", send):
        await NotionAdapter("token").fetch_page_blocks("page-1")
    assert send.await_args.args[3] == "https://notion.example.com/v1/blocks/page-1/children"
