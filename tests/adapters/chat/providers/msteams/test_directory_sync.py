"""Unit tests for Microsoft Teams directory synchronization."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from rls_test_helpers import data_statements

from checkcord.adapters.chat.providers.msteams.directory_sync import sync_msteams_directory


def create_mock_engine() -> MagicMock:
    """Create a mock database engine for testing directory sync."""
    mock_conn_connect = AsyncMock()
    mock_result = MagicMock()
    mock_result.fetchall.return_value = [
        MagicMock(
            ms_tenant_id="ms-tenant-456", tenant_id=UUID("22222222-2222-2222-2222-222222222222")
        )
    ]
    mock_conn_connect.execute.return_value = mock_result

    mock_engine = MagicMock()
    mock_engine.connect.return_value.__aenter__.return_value = mock_conn_connect
    mock_engine.begin.return_value.__aenter__.return_value = mock_conn_connect
    return mock_engine


class MockResponse:
    """Mock aiohttp HTTP response object."""

    def __init__(self, json_data: dict[str, Any], status: int = 200):
        self._json_data = json_data
        self.status = status

    async def json(self) -> dict[str, Any]:
        return self._json_data

    async def text(self) -> str:
        return "text"

    async def __aenter__(self) -> MockResponse:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


@pytest.mark.asyncio
async def test_sync_msteams_directory_success() -> None:
    """Verify that sync_msteams_directory queries APIs and upserts results."""
    engine = create_mock_engine()

    # Mock settings
    mock_settings = MagicMock()
    mock_settings.msteams_client_id = "test-client-id"
    mock_settings.msteams_client_secret = "test-client-secret"

    # Mock response data
    token_data = {"access_token": "mock-token-abc"}
    users_data = {
        "value": [
            {
                "id": "user-123",
                "displayName": "Jane Doe",
                "userPrincipalName": "jane@example.com",
            }
        ]
    }
    teams_data = {
        "value": [
            {
                "id": "team-789",
                "displayName": "Engineering Team",
            }
        ]
    }
    channels_data = {
        "value": [
            {
                "id": "channel-999",
                "displayName": "General",
            }
        ]
    }

    # Setup mocked GET/POST requests
    def mock_get(url: str, *args: Any, **kwargs: Any) -> MockResponse:
        if "users" in url:
            return MockResponse(users_data)
        elif "groups" in url:
            return MockResponse(teams_data)
        elif "channels" in url:
            return MockResponse(channels_data)
        return MockResponse({})

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.post.return_value = MockResponse(token_data)
    mock_session.get = MagicMock(side_effect=mock_get)

    with patch(
        "checkcord.adapters.chat.providers.msteams.directory_sync.get_settings",
        return_value=mock_settings,
    ):
        with patch("aiohttp.ClientSession", return_value=mock_session):
            await sync_msteams_directory(engine)

    # 1 read for installations, 3 upserts (users, groups, channels)
    conn = engine.begin.return_value.__aenter__.return_value
    verbs = [str(stmt).split()[0] for stmt in data_statements(conn)]
    assert verbs == ["SELECT", "INSERT", "INSERT", "INSERT", "UPDATE"]
    assert channels_data["value"][0]["team_id"] == "team-789"


@pytest.mark.asyncio
async def test_graph_listing_follows_next_links() -> None:
    from checkcord.adapters.chat.providers.msteams.directory_sync import _graph_list

    pages = {
        "first": MockResponse({"value": [{"id": "a"}], "@odata.nextLink": "second"}),
        "second": MockResponse({"value": [{"id": "b"}]}),
    }
    session = MagicMock()
    session.get = MagicMock(side_effect=lambda url: pages[url])

    items, status = await _graph_list(session, "first")

    assert [item["id"] for item in items] == ["a", "b"]
    assert status == 200


@pytest.mark.asyncio
async def test_graph_listing_reports_a_refused_page() -> None:
    from checkcord.adapters.chat.providers.msteams.directory_sync import _graph_list

    session = MagicMock()
    session.get = MagicMock(return_value=MockResponse({}, status=403))

    assert await _graph_list(session, "first") == ([], 403)
