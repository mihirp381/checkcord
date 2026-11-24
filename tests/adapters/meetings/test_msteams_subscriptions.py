from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fake_graph import FakeGraph
from fastapi import FastAPI
from fastapi.testclient import TestClient

from checkcord.adapters.meetings.providers.msteams import subscriptions
from checkcord.adapters.meetings.providers.msteams.webhook import router
from checkcord.core.config import Settings

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(
        environment="test",
        msteams_client_id="app-id",
        msteams_client_secret="app-" + "pw",
        public_base_url="https://checkcord.example.com/",
    )


async def test_create_asks_for_transcripts_with_lifecycle_and_client_state() -> None:
    graph = FakeGraph()
    async with graph.client() as client:
        sub = await subscriptions.create(client, "ms-tenant", "state-value", _settings(), NOW)
    stored = graph.subscriptions[sub.id]
    assert stored["resource"] == subscriptions.RESOURCE
    assert stored["changeType"] == "created"
    assert stored["clientState"] == "state-value"
    assert (
        stored["notificationUrl"]
        == "https://checkcord.example.com/api/v1/webhooks/msteams/meetings"
    )
    assert stored["lifecycleNotificationUrl"].endswith("/msteams/meetings/lifecycle")
    assert sub.expires_at == NOW + subscriptions.LIFETIME
    assert ("POST", "/ms-tenant/oauth2/v2.0/token") in graph.calls


async def test_graph_and_sign_in_use_the_configured_addresses() -> None:
    settings = _settings().model_copy(
        update={
            "msgraph_url": "https://graph.microsoft.us",
            "ms_login_url": "https://login.microsoftonline.us",
        }
    )
    graph = FakeGraph()
    async with graph.client() as client:
        sub = await subscriptions.create(client, "ms-tenant", "s", settings, NOW)
        await subscriptions.delete(client, "ms-tenant", sub.id, settings)
    assert graph.hosts == {"graph.microsoft.us", "login.microsoftonline.us"}
    assert ("POST", "/v1.0/subscriptions") in graph.calls


async def test_renew_extends_and_recreates_a_missing_subscription() -> None:
    graph = FakeGraph()
    async with graph.client() as client:
        sub = await subscriptions.create(client, "ms-tenant", "s", _settings(), NOW)
        later = NOW + timedelta(hours=50)
        renewed = await subscriptions.renew(client, "ms-tenant", sub.id, "s", _settings(), later)
        assert renewed.id == sub.id
        assert renewed.expires_at == later + subscriptions.LIFETIME

        graph.subscriptions.clear()
        again = await subscriptions.renew(client, "ms-tenant", sub.id, "s", _settings(), later)
    assert again.id != sub.id
    assert again.id in graph.subscriptions


async def test_rate_limited_graph_is_retried() -> None:
    graph = FakeGraph()
    graph.limited = 2
    with patch("asyncio.sleep", AsyncMock()):
        async with graph.client() as client:
            sub = await subscriptions.create(client, "ms-tenant", "s", _settings(), NOW)
    assert sub.id in graph.subscriptions


async def test_delete_tolerates_a_subscription_already_gone() -> None:
    graph = FakeGraph()
    async with graph.client() as client:
        sub = await subscriptions.create(client, "ms-tenant", "s", _settings(), NOW)
        await subscriptions.delete(client, "ms-tenant", sub.id, _settings())
        assert graph.subscriptions == {}
        await subscriptions.delete(client, "ms-tenant", sub.id, _settings())


async def test_missing_configuration_is_an_error_not_a_call() -> None:
    graph = FakeGraph()
    async with graph.client() as client:
        with pytest.raises(subscriptions.GraphError, match="MSTEAMS_CLIENT_ID"):
            await subscriptions.create(client, "t", "s", Settings(environment="test"), NOW)
    assert graph.calls == []


async def test_lifecycle_endpoint_echoes_the_validation_token() -> None:
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/webhooks/msteams/meetings/lifecycle?validationToken=check%20me", content=b""
        )
    assert resp.status_code == 200
    assert resp.text == "check me"
    assert resp.headers["content-type"].startswith("text/plain")
