"""Unit tests for Slack directory synchronization."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from rls_test_helpers import data_statements

from checkcord.adapters.chat.providers.slack.directory_sync import sync_slack_directory


def create_mock_engine() -> MagicMock:
    """Create a mock database engine for testing directory sync."""
    mock_conn_connect = AsyncMock()
    mock_result = MagicMock()
    mock_result.fetchall.return_value = [
        MagicMock(bot_token="xoxb-test", tenant_id=UUID("11111111-1111-1111-1111-111111111111"))
    ]
    mock_conn_connect.execute.return_value = mock_result

    mock_engine = MagicMock()
    mock_engine.connect.return_value.__aenter__.return_value = mock_conn_connect
    mock_engine.begin.return_value.__aenter__.return_value = mock_conn_connect
    return mock_engine


@pytest.mark.asyncio
async def test_sync_slack_directory_success() -> None:
    """Verify that sync_slack_directory queries Slack APIs and upserts results into the DB."""
    engine = create_mock_engine()

    # Mock Slack client responses
    mock_client = MagicMock()
    mock_client.users_list = AsyncMock(
        return_value={
            "ok": True,
            "members": [
                {
                    "id": "U123",
                    "deleted": False,
                    "profile": {"display_name": "bob", "email": "bob@example.com"},
                }
            ],
        }
    )
    mock_client.conversations_list = AsyncMock(
        return_value={
            "ok": True,
            "channels": [{"id": "C123", "name": "general"}],
        }
    )
    mock_client.usergroups_list = AsyncMock(
        return_value={
            "ok": True,
            "usergroups": [{"id": "S123", "name": "devs"}],
        }
    )

    with patch(
        "checkcord.adapters.chat.providers.slack.directory_sync.AsyncWebClient",
        return_value=mock_client,
    ):
        await sync_slack_directory(engine)

    assert mock_client.users_list.call_count == 1
    assert mock_client.conversations_list.call_count == 1
    assert mock_client.usergroups_list.call_count == 1

    # 1 read for installations, 3 upserts for users, channels, groups, then matching by email
    conn = engine.begin.return_value.__aenter__.return_value
    verbs = [str(stmt).split()[0] for stmt in data_statements(conn)]
    assert verbs == ["SELECT", "INSERT", "INSERT", "INSERT", "UPDATE"]


@pytest.mark.asyncio
async def test_sync_follows_cursors_and_keeps_poll_position() -> None:
    engine = create_mock_engine()
    mock_client = MagicMock()
    mock_client.users_list = AsyncMock(return_value={"ok": True, "members": []})
    mock_client.conversations_list = AsyncMock(
        side_effect=[
            {
                "ok": True,
                "channels": [{"id": "C1", "name": "one", "is_member": True}],
                "response_metadata": {"next_cursor": "page2"},
            },
            {
                "ok": True,
                "channels": [{"id": "C2", "name": "two", "is_member": False}],
                "response_metadata": {"next_cursor": ""},
            },
        ]
    )
    mock_client.usergroups_list = AsyncMock(return_value={"ok": True, "usergroups": []})

    with patch(
        "checkcord.adapters.chat.providers.slack.directory_sync.AsyncWebClient",
        return_value=mock_client,
    ):
        await sync_slack_directory(engine)

    cursors = [call.kwargs["cursor"] for call in mock_client.conversations_list.call_args_list]
    assert cursors == [None, "page2"]
    conn = engine.begin.return_value.__aenter__.return_value
    upserts = [str(stmt) for stmt in data_statements(conn) if str(stmt).startswith("INSERT")]
    assert len(upserts) == 2
    assert all("metadata = (chat_channels.metadata || excluded.metadata)" in s for s in upserts)
