from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app_role_helpers import create_app_role, drop_app_role
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.adapters.chat.providers.msteams.directory_sync import sync_msteams_directory
from checkcord.adapters.chat.providers.slack.directory_sync import sync_slack_directory
from checkcord.core.config import get_settings
from checkcord.core.crypto import encrypt_secret

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@dataclass(frozen=True)
class Workspace:
    tenant: UUID
    engine: AsyncEngine
    owner: AsyncEngine

    async def rows(self, table: str, platform: str) -> dict[str, tuple[str, dict[str, Any]]]:
        async with self.owner.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            result = await conn.execute(
                text(
                    f"SELECT native_id, display_name, metadata FROM {table} "  # noqa: S608
                    "WHERE tenant_id = :t AND platform = :p"
                ),
                {"t": self.tenant, "p": platform},
            )
            return {row.native_id: (row.display_name, row.metadata) for row in result}

    async def mark_polled(self, native_id: str, position: str) -> None:
        async with self.owner.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text(
                    "UPDATE chat_channels SET metadata = metadata || "
                    "jsonb_build_object('last_polled_ts', CAST(:pos AS text)) "
                    "WHERE tenant_id = :t AND native_id = :n"
                ),
                {"t": self.tenant, "n": native_id, "pos": position},
            )


@pytest_asyncio.fixture
async def workspace() -> AsyncGenerator[Workspace, None]:
    tenant = uuid4()
    slack_team, ms_tenant = f"T{uuid4().hex[:8]}", f"ms-{uuid4().hex[:8]}"
    owner = create_async_engine(get_settings().database_url)
    async with owner.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text(
                "INSERT INTO slack_installations (team_id, tenant_id, bot_token) "
                "VALUES (:s, :t, :b)"
            ),
            {"s": slack_team, "t": tenant, "b": encrypt_secret("xoxb-test")},
        )
        await conn.execute(
            text("INSERT INTO msteams_installations (ms_tenant_id, tenant_id) VALUES (:m, :t)"),
            {"m": ms_tenant, "t": tenant},
        )
    role, url = await create_app_role()
    engine = create_async_engine(url)
    try:
        yield Workspace(tenant, engine, owner)
    finally:
        await engine.dispose()
        await drop_app_role(role)
        async with owner.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            for table in (
                "chat_users",
                "chat_channels",
                "chat_groups",
                "slack_installations",
                "msteams_installations",
            ):
                await conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant})  # noqa: S608
        await owner.dispose()


def _slack_client(channel_name: str) -> MagicMock:
    client = MagicMock()
    client.users_list = AsyncMock(
        return_value={
            "ok": True,
            "members": [
                {"id": "U1", "real_name": "Dana", "profile": {"email": "dana@acme.example"}}
            ],
        }
    )
    client.conversations_list = AsyncMock(
        return_value={
            "ok": True,
            "channels": [{"id": "C1", "name": channel_name, "is_member": True}],
        }
    )
    client.usergroups_list = AsyncMock(
        return_value={
            "ok": True,
            "usergroups": [{"id": "S1", "name": "oncall", "handle": "oncall"}],
        }
    )
    return client


async def test_slack_sync_writes_and_rewrites_the_directory(workspace: Workspace) -> None:
    target = "checkcord.adapters.chat.providers.slack.directory_sync.AsyncWebClient"
    with patch(target, return_value=_slack_client("general")):
        await sync_slack_directory(workspace.engine)
    await workspace.mark_polled("C1", "1700000000.000100")
    with patch(target, return_value=_slack_client("general-renamed")):
        await sync_slack_directory(workspace.engine)

    assert set(await workspace.rows("chat_users", "slack")) == {"U1"}
    assert set(await workspace.rows("chat_groups", "slack")) == {"S1"}
    name, metadata = (await workspace.rows("chat_channels", "slack"))["C1"]
    assert name == "general-renamed"
    assert metadata["last_polled_ts"] == "1700000000.000100"
    assert metadata["is_member"] is True


class _GraphResponse:
    status = 200

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body

    async def __aenter__(self) -> "_GraphResponse":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _graph_session(channel_name: str) -> MagicMock:
    def get(url: str, *_args: Any, **_kwargs: Any) -> _GraphResponse:
        if url.endswith("/users"):
            return _GraphResponse({"value": [{"id": "u-1", "displayName": "Dana"}]})
        if "/groups" in url:
            return _GraphResponse({"value": [{"id": "team-1", "displayName": "Engineering"}]})
        return _GraphResponse({"value": [{"id": "chan-1", "displayName": channel_name}]})

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.get = MagicMock(side_effect=get)
    return session


async def test_teams_sync_writes_and_rewrites_the_directory(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MSTEAMS_CLIENT_ID", "client-id")
    monkeypatch.setenv("MSTEAMS_CLIENT_SECRET", "client-secret")
    get_settings.cache_clear()
    module = "checkcord.adapters.chat.providers.msteams.directory_sync"
    try:
        with patch(f"{module}.get_msteams_access_token", AsyncMock(return_value="token")):
            with patch(f"{module}.aiohttp.ClientSession", return_value=_graph_session("General")):
                await sync_msteams_directory(workspace.engine)
            await workspace.mark_polled("chan-1", "2026-01-01T00:00:00+00:00")
            with patch(f"{module}.aiohttp.ClientSession", return_value=_graph_session("Renamed")):
                await sync_msteams_directory(workspace.engine)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()

    assert set(await workspace.rows("chat_users", "ms_teams")) == {"u-1"}
    assert set(await workspace.rows("chat_groups", "ms_teams")) == {"team-1"}
    name, metadata = (await workspace.rows("chat_channels", "ms_teams"))["chan-1"]
    assert name == "Renamed"
    assert metadata["last_polled_ts"] == "2026-01-01T00:00:00+00:00"
    assert metadata["team_id"] == "team-1"
