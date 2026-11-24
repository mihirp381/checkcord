"""Checkcord keeps the Teams meetings Graph subscription: create, renew, handshake and clean-up."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fake_graph import FakeGraph
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.meetings.providers.msteams import subscriptions
from checkcord.adapters.meetings.providers.msteams.connector import connect_teams_meetings
from checkcord.adapters.meetings.providers.msteams.webhook import router
from checkcord.core.config import Settings, get_settings
from checkcord.services import connections, doctor
from checkcord.storage.models import MsTeamsMeetingInstallation
from checkcord.storage.postgres import create_checkcord_engine, tenant_context

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_checkcord_engine(get_settings().database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
def teams_app(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("MSTEAMS_CLIENT_ID", "app-id")
    monkeypatch.setenv("MSTEAMS_CLIENT_SECRET", "app-" + "pw")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://checkcord.example.com")
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def graph() -> FakeGraph:
    graph = FakeGraph()

    @asynccontextmanager
    async def fake(client: httpx.AsyncClient | None) -> AsyncIterator[httpx.AsyncClient]:
        async with graph.client() as http:
            yield http

    with patch.object(subscriptions, "_client", fake):
        yield graph


@asynccontextmanager
async def connected(engine: AsyncEngine) -> AsyncIterator[tuple[UUID, str]]:
    tenant, ms_tenant = uuid4(), f"ms-{uuid4().hex[:8]}"
    await connect_teams_meetings(engine, tenant, ms_tenant, rotate_secret=False)
    try:
        yield tenant, ms_tenant
    finally:
        async with tenant_context(engine, tenant) as conn:
            await conn.execute(
                text("DELETE FROM msteams_meeting_installations WHERE tenant_id = :t"),
                {"t": tenant},
            )


async def _row(engine: AsyncEngine, tenant: UUID) -> Any:
    async with tenant_context(engine, tenant) as conn:
        return (
            (
                await conn.execute(
                    select(MsTeamsMeetingInstallation).where(
                        MsTeamsMeetingInstallation.tenant_id == tenant
                    )
                )
            )
            .mappings()
            .one()
        )


async def _expire(engine: AsyncEngine, tenant: UUID, at: datetime) -> None:
    async with tenant_context(engine, tenant) as conn:
        await conn.execute(
            text(
                "UPDATE msteams_meeting_installations SET subscription_expires_at = :at "
                "WHERE tenant_id = :t"
            ),
            {"at": at, "t": tenant},
        )


async def test_connect_creates_and_the_job_renews_only_when_due(
    engine: AsyncEngine, teams_app: Settings, graph: FakeGraph
) -> None:
    async with connected(engine) as (tenant, ms_tenant):
        sub, error = await subscriptions.ensure(engine, tenant, ms_tenant)
        assert error is None and sub is not None
        row = await _row(engine, tenant)
        assert row["subscription_id"] == sub.id
        assert graph.subscriptions[sub.id]["clientState"] != ""

        calls = len(graph.calls)
        await subscriptions.renew_due(engine)
        assert not [c for c in graph.calls[calls:] if c[1].endswith(sub.id)]

        await _expire(engine, tenant, datetime.now(UTC) + timedelta(hours=2))
        await subscriptions.renew_due(engine)
        assert ("PATCH", f"/v1.0/subscriptions/{sub.id}") in graph.calls
        renewed = await _row(engine, tenant)
        assert renewed["subscription_id"] == sub.id
        assert renewed["subscription_expires_at"] > datetime.now(UTC) + timedelta(hours=60)

        graph.subscriptions.clear()
        await _expire(engine, tenant, datetime.now(UTC) + timedelta(hours=1))
        await subscriptions.renew_due(engine)
        recreated = await _row(engine, tenant)
        assert recreated["subscription_id"] != sub.id
        assert recreated["subscription_id"] in graph.subscriptions


async def test_a_failed_renewal_is_stored_and_shown_by_doctor(
    engine: AsyncEngine, teams_app: Settings, graph: FakeGraph
) -> None:
    async with connected(engine) as (tenant, ms_tenant):
        graph.fail_token = True
        sub, error = await subscriptions.ensure(engine, tenant, ms_tenant)
        assert sub is None and error == "token request answered 401"
        check = await doctor.teams_subscription_check(engine, tenant)
        assert check is not None and not check.ok
        assert "token request answered 401" in check.detail

        graph.fail_token = False
        await subscriptions.renew_due(engine)
        row = await _row(engine, tenant)
        assert row["subscription_error"] is None and row["subscription_id"] in graph.subscriptions
        check = await doctor.teams_subscription_check(engine, tenant)
        assert check is not None and check.ok and "1 live" in check.detail

    assert await doctor.teams_subscription_check(engine, tenant) is None


async def test_lifecycle_notifications_renew_or_recreate(
    engine: AsyncEngine, teams_app: Settings, graph: FakeGraph
) -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.ctx = SimpleNamespace(db_engine=engine, redis_client=None)
    async with connected(engine) as (tenant, ms_tenant):
        sub, _ = await subscriptions.ensure(engine, tenant, ms_tenant)
        assert sub is not None
        state = graph.subscriptions[sub.id]["clientState"]

        def note(event: str, client_state: str = state) -> dict[str, Any]:
            return {
                "value": [
                    {
                        "subscriptionId": sub.id,
                        "tenantId": ms_tenant,
                        "clientState": client_state,
                        "lifecycleEvent": event,
                    }
                ]
            }

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            url = "/api/v1/webhooks/msteams/meetings/lifecycle"
            handshake = await client.post(f"{url}?validationToken=abc123")
            assert handshake.status_code == 200 and handshake.text == "abc123"

            calls = len(graph.calls)
            assert (
                await client.post(url, json=note("reauthorizationRequired", "wrong"))
            ).status_code == 202
            assert graph.calls[calls:] == []

            assert (await client.post(url, json=note("reauthorizationRequired"))).status_code == 202
            assert ("PATCH", f"/v1.0/subscriptions/{sub.id}") in graph.calls[calls:]

            graph.subscriptions.clear()
            assert (await client.post(url, json=note("subscriptionRemoved"))).status_code == 202
            row = await _row(engine, tenant)
            assert row["subscription_id"] != sub.id
            assert row["subscription_id"] in graph.subscriptions


async def test_disconnect_deletes_the_subscription(
    engine: AsyncEngine, teams_app: Settings, graph: FakeGraph
) -> None:
    async with connected(engine) as (tenant, ms_tenant):
        sub, _ = await subscriptions.ensure(engine, tenant, ms_tenant)
        assert sub is not None and sub.id in graph.subscriptions
        assert await connections.disconnect(engine, tenant, "teams-meetings", ms_tenant)
        assert graph.subscriptions == {}
        assert ("DELETE", f"/v1.0/subscriptions/{sub.id}") in graph.calls
