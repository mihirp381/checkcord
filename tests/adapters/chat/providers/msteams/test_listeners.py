"""Unit tests for MS Teams activity event listeners and normalization."""

from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from checkcord.main import app


@pytest.fixture
def mock_ctx() -> MagicMock:
    """Fixture to mock AppContext database engine and Redis client."""
    ctx = MagicMock()
    ctx.db_engine = MagicMock()
    ctx.redis_client = AsyncMock()

    # Mock database connection results for tenant_id lookup
    mock_conn = AsyncMock()
    mock_result = MagicMock()
    mock_result.fetchone.return_value = (UUID("22222222-2222-2222-2222-222222222222"),)
    mock_conn.execute.return_value = mock_result
    ctx.db_engine.connect.return_value.__aenter__.return_value = mock_conn
    ctx.db_engine.begin.return_value.__aenter__.return_value = mock_conn

    # Setup redis return value for xadd
    ctx.redis_client.xadd.return_value = b"msg-id-123"

    return ctx


@pytest.fixture(autouse=True)
def msteams_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSTEAMS_CLIENT_ID", "ms-client-123")
    monkeypatch.setenv("MSTEAMS_CLIENT_SECRET", "ms-secret-123")


@pytest.fixture(autouse=True)
def patch_app_lifecycle(mock_ctx: MagicMock) -> Generator[None, None, None]:
    """Automatically patch the FastAPI app_lifecycle context manager."""

    @asynccontextmanager
    async def mock_lifecycle() -> AsyncGenerator[MagicMock, None]:
        yield mock_ctx

    with patch("checkcord.main.app_lifecycle", mock_lifecycle):
        yield


def test_msteams_listener_new_message(mock_ctx: MagicMock) -> None:
    """Test MS Teams endpoint parses, normalizes and publishes new messages."""
    app.state.ctx = mock_ctx

    activity = {
        "type": "message",
        "id": "activity-123",
        "timestamp": "2026-06-22T04:44:00Z",
        "tenantId": "ms-tenant-456",
        "from": {"id": "user-789", "name": "Jane Doe", "role": "user"},
        "conversation": {"id": "conversation-999"},
        "text": '<p>Hello <at id="alice-id">Alice</at></p>',
    }

    with TestClient(app) as client:
        response = client.post("/api/messages", json=activity)
        assert response.status_code == 200

    assert mock_ctx.redis_client.xadd.call_count == 1
    call_args = mock_ctx.redis_client.xadd.call_args
    assert call_args[1]["name"] == "events.raw.chat"

    payload = call_args[1]["fields"]
    assert payload["platform"] == "ms_teams"
    assert payload["content_raw"] == '<p>Hello <at id="alice-id">Alice</at></p>'
    assert payload["content_clean"] == "Hello @Alice"
    assert payload["tenant_id"] == "22222222-2222-2222-2222-222222222222"
    assert payload["message_id"] == "activity-123"


def test_msteams_listener_message_edit(mock_ctx: MagicMock) -> None:
    """Test MS Teams edits map to updated text content."""
    app.state.ctx = mock_ctx

    activity = {
        "type": "message",
        "id": "activity-123",
        "timestamp": "2026-06-22T04:45:00Z",
        "tenantId": "ms-tenant-456",
        "from": {"id": "user-789", "name": "Jane Doe"},
        "conversation": {"id": "conversation-999"},
        "channelData": {"eventType": "editMessage"},
        "text": "<p>Updated Text</p>",
    }

    with TestClient(app) as client:
        response = client.post("/api/messages", json=activity)
        assert response.status_code == 200

    assert mock_ctx.redis_client.xadd.call_count == 1
    payload = mock_ctx.redis_client.xadd.call_args[1]["fields"]
    assert payload["content_clean"] == "Updated Text"
    assert payload["message_id"] == "activity-123"


def test_msteams_listener_message_update_type(mock_ctx: MagicMock) -> None:
    """Test MS Teams activity of type messageUpdate maps updated text."""
    app.state.ctx = mock_ctx

    activity = {
        "type": "messageUpdate",
        "id": "activity-123",
        "timestamp": "2026-06-22T04:45:00Z",
        "tenantId": "ms-tenant-456",
        "from": {"id": "user-789", "name": "Jane Doe"},
        "conversation": {"id": "conversation-999"},
        "text": "<p>Updated Text via messageUpdate</p>",
    }

    with TestClient(app) as client:
        response = client.post("/api/messages", json=activity)
        assert response.status_code == 200

    assert mock_ctx.redis_client.xadd.call_count == 1
    payload = mock_ctx.redis_client.xadd.call_args[1]["fields"]
    assert payload["content_clean"] == "Updated Text via messageUpdate"
    assert payload["message_id"] == "activity-123"


def test_msteams_listener_message_delete(mock_ctx: MagicMock) -> None:
    """Test MS Teams deletes are mapped to empty content in raw payload."""
    app.state.ctx = mock_ctx

    activity = {
        "type": "messageDelete",
        "id": "activity-123",
        "timestamp": "2026-06-22T04:46:00Z",
        "tenantId": "ms-tenant-456",
        "from": {"id": "user-789"},
        "conversation": {"id": "conversation-999"},
    }

    with TestClient(app) as client:
        response = client.post("/api/messages", json=activity)
        assert response.status_code == 200

    assert mock_ctx.redis_client.xadd.call_count == 1
    payload = mock_ctx.redis_client.xadd.call_args[1]["fields"]
    assert payload["content_raw"] == ""
    assert payload["content_clean"] == ""
    assert payload["message_id"] == "activity-123"


def test_msteams_listener_threaded_reply(mock_ctx: MagicMock) -> None:
    """Test MS Teams threaded reply populates thread_ts."""
    app.state.ctx = mock_ctx

    activity = {
        "type": "message",
        "id": "reply-456",
        "replyToId": "root-123",
        "timestamp": "2026-06-22T04:44:30Z",
        "tenantId": "ms-tenant-456",
        "from": {"id": "user-789"},
        "conversation": {"id": "conversation-999"},
        "text": "Reply text",
    }

    with TestClient(app) as client:
        response = client.post("/api/messages", json=activity)
        assert response.status_code == 200

    assert mock_ctx.redis_client.xadd.call_count == 1
    payload = mock_ctx.redis_client.xadd.call_args[1]["fields"]
    assert payload["thread_ts"] == "root-123"


def test_msteams_listener_unregistered_tenant(mock_ctx: MagicMock) -> None:
    """Test that unregistered tenant is logged as warning and returns 200 OK (discards event)."""
    app.state.ctx = mock_ctx

    # Mock database to return no tenant_id
    mock_conn = AsyncMock()
    mock_result = MagicMock()
    mock_result.fetchone.return_value = None
    mock_conn.execute.return_value = mock_result
    mock_ctx.db_engine.connect.return_value.__aenter__.return_value = mock_conn
    mock_ctx.db_engine.begin.return_value.__aenter__.return_value = mock_conn

    activity = {
        "type": "message",
        "id": "activity-123",
        "timestamp": "2026-06-22T04:44:00Z",
        "tenantId": "unregistered-tenant",
        "from": {"id": "user-789"},
        "conversation": {"id": "conversation-999"},
        "text": "Hello World",
    }

    with TestClient(app) as client:
        response = client.post("/api/messages", json=activity)
        assert response.status_code == 200
        assert "Tenant not found, event discarded" in response.text

    # Verify event is NOT published to Redis
    assert mock_ctx.redis_client.xadd.call_count == 0


def test_msteams_listener_unsupported_activity_type(mock_ctx: MagicMock) -> None:
    """Test that unsupported activity types (e.g. conversationUpdate) are ignored with 200 OK."""
    app.state.ctx = mock_ctx

    activity = {
        "type": "conversationUpdate",
        "id": "activity-123",
        "timestamp": "2026-06-22T04:44:00Z",
        "tenantId": "ms-tenant-456",
    }

    with TestClient(app) as client:
        response = client.post("/api/messages", json=activity)
        assert response.status_code == 200

    # Verify event is NOT published to Redis
    assert mock_ctx.redis_client.xadd.call_count == 0


def test_msteams_listener_bot_filtering(mock_ctx: MagicMock) -> None:
    """Verify bot noise filtering in MS Teams listener."""
    app.state.ctx = mock_ctx

    with TestClient(app) as client:
        # 1. Filtered by blocked bot role/id
        activity_bot = {
            "type": "message",
            "id": "activity-bot-123",
            "timestamp": "2026-06-22T04:44:00Z",
            "tenantId": "ms-tenant-456",
            "from": {"id": "uslackbot", "name": "Some Bot", "role": "bot"},
            "conversation": {"id": "conversation-999"},
            "text": "Hello World",
        }
        response = client.post("/api/messages", json=activity_bot)
        assert response.status_code == 200
        assert mock_ctx.redis_client.xadd.call_count == 0

        # 2. Filtered by blocked username (name)
        activity_user = {
            "type": "message",
            "id": "activity-user-123",
            "timestamp": "2026-06-22T04:44:00Z",
            "tenantId": "ms-tenant-456",
            "from": {"id": "user-789", "name": "datadog", "role": "user"},
            "conversation": {"id": "conversation-999"},
            "text": "System metrics normal",
        }
        response = client.post("/api/messages", json=activity_user)
        assert response.status_code == 200
        assert mock_ctx.redis_client.xadd.call_count == 0

        # 3. Filtered by blocked content pattern
        activity_text = {
            "type": "message",
            "id": "activity-text-123",
            "timestamp": "2026-06-22T04:44:00Z",
            "tenantId": "ms-tenant-456",
            "from": {"id": "user-789", "name": "Jane Doe", "role": "user"},
            "conversation": {"id": "conversation-999"},
            "text": "[sentry] division by zero",
        }
        response = client.post("/api/messages", json=activity_text)
        assert response.status_code == 200
        assert mock_ctx.redis_client.xadd.call_count == 0

        # 4. Deleted message with blocked fields is NOT filtered
        activity_deleted = {
            "type": "messageDelete",
            "id": "activity-deleted-123",
            "timestamp": "2026-06-22T04:44:00Z",
            "tenantId": "ms-tenant-456",
            "from": {"id": "uslackbot", "name": "datadog", "role": "bot"},
            "conversation": {"id": "conversation-999"},
            "text": "[sentry] Original text",
        }
        response = client.post("/api/messages", json=activity_deleted)
        assert response.status_code == 200
        assert mock_ctx.redis_client.xadd.call_count == 1


class MockAiohttpResponse:
    def __init__(
        self,
        json_data: dict[str, Any],
        status: int = 200,
        bytes_data: bytes = b"",
        headers: dict[str, Any] | None = None,
    ):
        self._json_data = json_data
        self.status = status
        self._bytes_data = bytes_data
        self.headers = headers or {}

    async def json(self) -> dict[str, Any]:
        return self._json_data

    async def read(self) -> bytes:
        return self._bytes_data

    async def text(self) -> str:
        return self._bytes_data.decode("utf-8")

    async def __aenter__(self) -> "MockAiohttpResponse":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


@pytest.mark.asyncio
async def test_msteams_listener_private_group_chat(mock_ctx: MagicMock) -> None:
    """Test private group chat detection, members fetching, and caching."""
    app.state.ctx = mock_ctx

    activity = {
        "type": "message",
        "id": "activity-group-123",
        "timestamp": "2026-06-22T04:44:00Z",
        "tenantId": "ms-tenant-456",
        "from": {"id": "user-789", "name": "Jane Doe", "role": "user"},
        "conversation": {"id": "chat-group-999", "conversationType": "groupChat"},
        "text": "Hello group chat",
    }

    # Redis mocks
    mock_ctx.redis_client.get.return_value = None  # Not cached

    # Graph API Mock Response for listing members
    members_response = {
        "value": [
            {"userId": "aad-user-1"},
            {"userId": "aad-user-2"},
        ]
    }

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.get.return_value = MockAiohttpResponse(members_response)

    resolved_internal_ids = [
        UUID("33333333-3333-3333-3333-333333333333"),
        UUID("44444444-4444-4444-4444-444444444444"),
    ]

    with (
        patch(
            "checkcord.adapters.chat.providers.msteams.directory_sync.get_msteams_access_token",
            AsyncMock(return_value="mock-token"),
        ),
        patch("aiohttp.ClientSession", return_value=mock_session),
        patch(
            "checkcord.services.accounts.match",
            AsyncMock(return_value=(dict(enumerate(resolved_internal_ids)), [])),
        ) as mock_resolve,
    ):
        with TestClient(app) as client:
            response = client.post("/api/messages", json=activity)
            assert response.status_code == 200

        engine, tenant, platform, native_ids, _ = mock_resolve.call_args.args
        assert engine is mock_ctx.db_engine
        assert tenant == UUID("22222222-2222-2222-2222-222222222222")
        assert platform == "ms_teams"
        assert list(native_ids) == ["aad-user-1", "aad-user-2"]

        # Assert xadd was called with channel_id
        assert mock_ctx.redis_client.xadd.call_count == 1
        payload = mock_ctx.redis_client.xadd.call_args[1]["fields"]
        assert payload["channel_id"] == "chat-group-999"


@pytest.mark.asyncio
async def test_msteams_listener_private_channel(mock_ctx: MagicMock) -> None:
    """Test private channel detection, database team_id lookup and members fetching."""
    app.state.ctx = mock_ctx

    activity = {
        "type": "message",
        "id": "activity-chan-123",
        "timestamp": "2026-06-22T04:44:00Z",
        "tenantId": "ms-tenant-456",
        "from": {"id": "user-789", "name": "Jane Doe", "role": "user"},
        "conversation": {"id": "channel-999", "conversationType": "channel"},
        "text": "Hello private channel",
    }

    # Mock database to return channel metadata with team_id
    mock_conn = AsyncMock()

    def execute_side_effect(stmt: Any, *args: Any, **kwargs: Any) -> MagicMock:
        stmt_str = str(stmt).lower()
        if "chat_users" in stmt_str or "enterprise_users" in stmt_str:
            res = MagicMock()
            res.fetchone.return_value = None
            return res
        elif "chat_channels" in stmt_str:
            res = MagicMock()
            res.fetchone.return_value = ({"team_id": "team-123"},)
            return res
        else:
            res = MagicMock()
            res.fetchone.return_value = (UUID("22222222-2222-2222-2222-222222222222"),)
            return res

    mock_conn.execute.side_effect = execute_side_effect
    mock_ctx.db_engine.connect.return_value.__aenter__.return_value = mock_conn
    mock_ctx.db_engine.begin.return_value.__aenter__.return_value = mock_conn

    # Redis mocks: get returns None for type and members
    mock_ctx.redis_client.get.return_value = None

    # Graph API Mocks
    channel_details = {"membershipType": "private"}
    members_response = {
        "value": [
            {"userId": "aad-user-3"},
        ]
    }

    # Define side effect for session.get
    def mock_get(url: str, *args: Any, **kwargs: Any) -> MockAiohttpResponse:
        if "channels/channel-999/members" in url:
            return MockAiohttpResponse(members_response)
        elif "channels/channel-999" in url:
            return MockAiohttpResponse(channel_details)
        return MockAiohttpResponse({})

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.get.side_effect = mock_get

    resolved_internal_ids = [UUID("55555555-5555-5555-5555-555555555555")]

    with (
        patch(
            "checkcord.adapters.chat.providers.msteams.directory_sync.get_msteams_access_token",
            AsyncMock(return_value="mock-token"),
        ),
        patch("aiohttp.ClientSession", return_value=mock_session),
        patch(
            "checkcord.services.accounts.match",
            AsyncMock(return_value=(dict(enumerate(resolved_internal_ids)), [])),
        ) as mock_resolve,
    ):
        with TestClient(app) as client:
            response = client.post("/api/messages", json=activity)
            assert response.status_code == 200

        engine, tenant, platform, native_ids, _ = mock_resolve.call_args.args
        assert engine is mock_ctx.db_engine
        assert tenant == UUID("22222222-2222-2222-2222-222222222222")
        assert platform == "ms_teams"
        assert list(native_ids) == ["aad-user-3"]

        # Assert xadd was called with channel_id
        assert mock_ctx.redis_client.xadd.call_count == 1
        payload = mock_ctx.redis_client.xadd.call_args[1]["fields"]
        assert payload["channel_id"] == "channel-999"


@pytest.mark.asyncio
async def test_msteams_listener_public_channel(mock_ctx: MagicMock) -> None:
    """Test public channel behaves as public."""
    app.state.ctx = mock_ctx

    activity = {
        "type": "message",
        "id": "activity-pub-123",
        "timestamp": "2026-06-22T04:44:00Z",
        "tenantId": "ms-tenant-456",
        "from": {"id": "user-789", "name": "Jane Doe", "role": "user"},
        "conversation": {"id": "channel-pub-999", "conversationType": "channel"},
        "text": "Hello public channel",
    }

    # Redis mocks: cached as public
    mock_ctx.redis_client.get.return_value = b"public"

    with TestClient(app) as client:
        response = client.post("/api/messages", json=activity)
        assert response.status_code == 200

    # Assert xadd was called with channel_id
    assert mock_ctx.redis_client.xadd.call_count == 1
    payload = mock_ctx.redis_client.xadd.call_args[1]["fields"]
    assert payload["channel_id"] == "channel-pub-999"


@pytest.mark.asyncio
async def test_msteams_listener_text_attachment_download_success(mock_ctx: MagicMock) -> None:
    """Verify downloading text attachments <= 5MB succeeds and appends to content_raw."""
    app.state.ctx = mock_ctx

    activity = {
        "type": "message",
        "id": "activity-att-123",
        "timestamp": "2026-06-22T04:44:00Z",
        "tenantId": "ms-tenant-456",
        "from": {"id": "user-789", "name": "Jane Doe", "role": "user"},
        "conversation": {"id": "channel-pub-999", "conversationType": "channel"},
        "text": "Check out this file:",
        "attachments": [
            {
                "contentType": "text/plain",
                "name": "notes.txt",
                "contentUrl": "https://example.com/notes.txt",
                "size": 100,  # <= 5MB
            }
        ],
    }

    # Redis mocks: cached as public to bypass member checks easily
    mock_ctx.redis_client.get.return_value = b"public"

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.get.return_value = MockAiohttpResponse({}, bytes_data=b"These are meeting notes.")

    with (
        patch(
            "checkcord.adapters.chat.providers.msteams.directory_sync.get_msteams_access_token",
            AsyncMock(return_value="mock-token"),
        ),
        patch("aiohttp.ClientSession", return_value=mock_session),
    ):
        with TestClient(app) as client:
            response = client.post("/api/messages", json=activity)
            assert response.status_code == 200

    assert mock_ctx.redis_client.xadd.call_count == 1
    payload = mock_ctx.redis_client.xadd.call_args[1]["fields"]
    assert "=== Attachment: notes.txt ===" in payload["content_raw"]
    assert "These are meeting notes." in payload["content_raw"]
    assert (
        "Check out this file:\n\n=== Attachment: notes.txt ===\nThese are meeting notes."
        == payload["content_raw"]
    )
    assert "These are meeting notes." in payload["content_clean"]


@pytest.mark.asyncio
async def test_msteams_listener_text_attachment_size_limit(mock_ctx: MagicMock) -> None:
    """Verify attachments exceeding 5MB are skipped."""
    app.state.ctx = mock_ctx

    activity = {
        "type": "message",
        "id": "activity-att-large",
        "timestamp": "2026-06-22T04:44:00Z",
        "tenantId": "ms-tenant-456",
        "from": {"id": "user-789", "name": "Jane Doe", "role": "user"},
        "conversation": {"id": "channel-pub-999", "conversationType": "channel"},
        "text": "Too big:",
        "attachments": [
            {
                "contentType": "text/plain",
                "name": "huge.txt",
                "contentUrl": "https://example.com/huge.txt",
                "size": 6 * 1024 * 1024,  # > 5MB
            }
        ],
    }

    mock_ctx.redis_client.get.return_value = b"public"

    # We mock ClientSession.get to verify it isn't called or is skipped.
    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "checkcord.adapters.chat.providers.msteams.directory_sync.get_msteams_access_token",
            AsyncMock(return_value="mock-token"),
        ),
        patch("aiohttp.ClientSession", return_value=mock_session),
    ):
        with TestClient(app) as client:
            response = client.post("/api/messages", json=activity)
            assert response.status_code == 200

    assert mock_ctx.redis_client.xadd.call_count == 1
    payload = mock_ctx.redis_client.xadd.call_args[1]["fields"]
    assert "huge.txt" not in payload["content_raw"]
    assert payload["content_raw"] == "Too big:"


@pytest.mark.asyncio
async def test_msteams_listener_text_attachment_size_limit_from_response(
    mock_ctx: MagicMock,
) -> None:
    """Verify attachments exceeding 5MB in response are skipped."""
    app.state.ctx = mock_ctx

    activity = {
        "type": "message",
        "id": "activity-att-large-resp",
        "timestamp": "2026-06-22T04:44:00Z",
        "tenantId": "ms-tenant-456",
        "from": {"id": "user-789", "name": "Jane Doe", "role": "user"},
        "conversation": {"id": "channel-pub-999", "conversationType": "channel"},
        "text": "Response is too big:",
        "attachments": [
            {
                "contentType": "text/plain",
                "name": "huge_resp.txt",
                "contentUrl": "https://example.com/huge_resp.txt",
            }
        ],
    }

    mock_ctx.redis_client.get.return_value = b"public"

    # Mock response with large Content-Length header
    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.get.return_value = MockAiohttpResponse(
        {}, bytes_data=b"a" * (6 * 1024 * 1024), headers={"Content-Length": str(6 * 1024 * 1024)}
    )

    with (
        patch(
            "checkcord.adapters.chat.providers.msteams.directory_sync.get_msteams_access_token",
            AsyncMock(return_value="mock-token"),
        ),
        patch("aiohttp.ClientSession", return_value=mock_session),
    ):
        with TestClient(app) as client:
            response = client.post("/api/messages", json=activity)
            assert response.status_code == 200

    assert mock_ctx.redis_client.xadd.call_count == 1
    payload = mock_ctx.redis_client.xadd.call_args[1]["fields"]
    assert "huge_resp.txt" not in payload["content_raw"]
    assert payload["content_raw"] == "Response is too big:"


@pytest.mark.asyncio
async def test_msteams_listener_text_attachment_download_failure(mock_ctx: MagicMock) -> None:
    """Verify failed download logs an error, appends warning, and still ingests."""
    app.state.ctx = mock_ctx

    activity = {
        "type": "message",
        "id": "activity-att-fail",
        "timestamp": "2026-06-22T04:44:00Z",
        "tenantId": "ms-tenant-456",
        "from": {"id": "user-789", "name": "Jane Doe", "role": "user"},
        "conversation": {"id": "channel-pub-999", "conversationType": "channel"},
        "text": "Failed download test:",
        "attachments": [
            {
                "contentType": "text/plain",
                "name": "broken.txt",
                "contentUrl": "https://example.com/broken.txt",
            }
        ],
    }

    mock_ctx.redis_client.get.return_value = b"public"

    # Mock response returning status 500
    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.get.return_value = MockAiohttpResponse({}, status=500)

    with (
        patch(
            "checkcord.adapters.chat.providers.msteams.directory_sync.get_msteams_access_token",
            AsyncMock(return_value="mock-token"),
        ),
        patch("aiohttp.ClientSession", return_value=mock_session),
    ):
        with TestClient(app) as client:
            response = client.post("/api/messages", json=activity)
            assert response.status_code == 200

    assert mock_ctx.redis_client.xadd.call_count == 1
    payload = mock_ctx.redis_client.xadd.call_args[1]["fields"]
    assert "broken.txt" in payload["content_raw"]
    assert "[Warning: Failed to download attachment broken.txt]" in payload["content_raw"]
    assert (
        payload["content_raw"]
        == "Failed download test:\n\n[Warning: Failed to download attachment broken.txt]"
    )


def test_msteams_listener_uses_aad_ids(mock_ctx: MagicMock) -> None:
    from checkcord.models.common import AccessControl

    app.state.ctx = mock_ctx
    activity = {
        "type": "message",
        "id": "activity-aad-1",
        "timestamp": "2026-06-22T04:44:00Z",
        "tenantId": "ms-tenant-456",
        "from": {"id": "29:1bf-id", "name": "Jane Doe", "aadObjectId": "aad-user-1"},
        "conversation": {
            "id": "19:chan@thread.tacv2;messageid=activity-aad-1",
            "conversationType": "channel",
        },
        "channelData": {
            "team": {"id": "19:general@thread.tacv2", "aadGroupId": "group-guid-1"},
            "channel": {"id": "19:chan@thread.tacv2"},
        },
        "text": "Hello",
    }
    audience = AsyncMock(return_value=AccessControl(is_public=True))
    with (
        patch(
            "checkcord.adapters.chat.providers.msteams.listeners.resolve_msteams_audience",
            audience,
        ),
        TestClient(app) as client,
    ):
        assert client.post("/api/messages", json=activity).status_code == 200

    assert audience.await_args.kwargs["team_id"] == "group-guid-1"
    assert audience.await_args.kwargs["channel_id"] == "19:chan@thread.tacv2"
    payload = mock_ctx.redis_client.xadd.call_args[1]["fields"]
    assert payload["author_id"] == "aad-user-1"
