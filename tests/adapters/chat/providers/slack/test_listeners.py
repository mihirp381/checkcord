"""Unit tests for Slack event listeners and message normalization."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from rls_test_helpers import data_statements

from checkcord.adapters.chat.providers.slack.listeners import (
    extract_text_from_blocks,
    is_generic_or_empty,
    register_listeners,
)


class MockAsyncApp:
    """Mock Bolt AsyncApp that records event listener functions."""

    def __init__(self) -> None:
        self.listeners: dict[str, Any] = {}
        self.actions: dict[str, Any] = {}

    def event(self, event_name: str) -> Any:
        def decorator(func: Any) -> Any:
            self.listeners[event_name] = func
            return func

        return decorator

    def action(self, action_name: str) -> Any:
        def decorator(func: Any) -> Any:
            self.actions[action_name] = func
            return func

        return decorator

    def view(self, view_name: str) -> Any:
        def decorator(func: Any) -> Any:
            self.listeners[view_name] = func
            return func

        return decorator


@pytest.fixture(autouse=True)
def slack_client() -> Any:
    client = AsyncMock()
    client.conversations_info.return_value = {"ok": True, "channel": {"is_private": False}}
    client.conversations_members.return_value = {
        "ok": True,
        "members": [],
        "response_metadata": {},
    }
    module = "checkcord.adapters.chat.providers.slack.listeners"
    with (
        patch(f"{module}.decrypt_secret", side_effect=lambda value: value),
        patch(f"{module}.AsyncWebClient", return_value=client),
    ):
        yield client


def create_mock_engine() -> MagicMock:
    """Helper to create a mocked SQLAlchemy AsyncEngine."""
    mock_conn = AsyncMock()
    mock_result = MagicMock()
    mock_result.fetchone.return_value = (UUID("11111111-1111-1111-1111-111111111111"), "stored")
    mock_conn.execute.return_value = mock_result

    mock_engine = MagicMock()
    mock_engine.connect.return_value.__aenter__.return_value = mock_conn
    mock_engine.begin.return_value.__aenter__.return_value = mock_conn
    return mock_engine


def test_extract_text_from_blocks() -> None:
    """Verify recursive text extraction from Slack block lists."""
    # 1. Simple section block
    blocks_simple = [{"type": "section", "text": {"type": "mrkdwn", "text": "Hello world!"}}]
    assert extract_text_from_blocks(blocks_simple) == "Hello world!"

    # 2. Rich text block with nested elements
    blocks_rich = [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [
                        {"type": "text", "text": "Hello "},
                        {"type": "text", "text": "world"},
                    ],
                }
            ],
        }
    ]
    assert extract_text_from_blocks(blocks_rich) == "Hello world"

    # 3. Empty list
    assert extract_text_from_blocks([]) == ""


def test_is_generic_or_empty() -> None:
    """Verify detection of generic placeholder message content."""
    assert is_generic_or_empty("") is True
    assert is_generic_or_empty("   ") is True
    assert is_generic_or_empty("This message has no text") is True
    assert is_generic_or_empty("message with blocks") is True
    assert is_generic_or_empty("Hello") is False


@pytest.mark.asyncio
async def test_slack_listener_new_message() -> None:
    """Test normalized RawChatPayload generation for a standard Slack message."""
    app = MockAsyncApp()
    engine = create_mock_engine()
    event_bus = AsyncMock()

    register_listeners(app=app, bus=event_bus, db_engine=engine)
    handler = app.listeners["message"]

    event = {
        "type": "message",
        "ts": "1612345678.000200",
        "channel": "C999",
        "team": "T12345",
        "user": "U12345",
        "text": "Hello <@U999>",
    }

    await handler(event)

    assert event_bus.publish.call_count == 1
    call_args = event_bus.publish.call_args
    topic = call_args[1]["topic"]
    published_payload = call_args[1]["event"]

    assert topic == "events.raw.chat"
    assert published_payload.content_raw == "Hello <@U999>"
    assert published_payload.content_clean == "Hello @U999"
    assert published_payload.platform == "slack"
    assert published_payload.channel_id == "C999"
    assert published_payload.message_id == "1612345678.000200"
    assert published_payload.author_id == "U12345"


@pytest.mark.asyncio
async def test_slack_listener_message_with_blocks() -> None:
    """Test text fallback to Block Kit when text field is generic."""
    app = MockAsyncApp()
    engine = create_mock_engine()
    event_bus = AsyncMock()

    register_listeners(app=app, bus=event_bus, db_engine=engine)
    handler = app.listeners["message"]

    event = {
        "type": "message",
        "ts": "1612345678.000200",
        "channel": "C999",
        "team": "T12345",
        "user": "U12345",
        "text": "This message has no text",
        "blocks": [
            {
                "type": "section",
                "text": {"type": "plain_text", "text": "Extracted from blocks"},
            }
        ],
    }

    await handler(event)

    assert event_bus.publish.call_count == 1
    published_payload = event_bus.publish.call_args[1]["event"]
    assert published_payload.content_raw == "Extracted from blocks"


@pytest.mark.asyncio
async def test_slack_listener_message_edited() -> None:
    """Test edit events update message payload content."""
    app = MockAsyncApp()
    engine = create_mock_engine()
    event_bus = AsyncMock()

    register_listeners(app=app, bus=event_bus, db_engine=engine)
    handler = app.listeners["message"]

    event = {
        "type": "message",
        "subtype": "message_changed",
        "ts": "1612345680.000300",
        "channel": "C999",
        "team": "T12345",
        "message": {
            "type": "message",
            "user": "U12345",
            "text": "Edited text",
            "ts": "1612345678.000200",
            "edited": {"user": "U12345", "ts": "1612345680.000000"},
        },
    }

    await handler(event)

    assert event_bus.publish.call_count == 1
    published_payload = event_bus.publish.call_args[1]["event"]
    assert published_payload.content_clean == "Edited text"
    assert published_payload.message_id == "1612345678.000200"


@pytest.mark.asyncio
async def test_slack_listener_message_deleted() -> None:
    """Test delete events emit raw payload with empty content."""
    app = MockAsyncApp()
    engine = create_mock_engine()
    event_bus = AsyncMock()

    register_listeners(app=app, bus=event_bus, db_engine=engine)
    handler = app.listeners["message"]

    event = {
        "type": "message",
        "subtype": "message_deleted",
        "ts": "1612345688.000400",
        "channel": "C999",
        "team": "T12345",
        "deleted_ts": "1612345678.000200",
        "previous_message": {
            "type": "message",
            "user": "U12345",
            "text": "Original text",
            "ts": "1612345678.000200",
        },
    }

    await handler(event)

    assert event_bus.publish.call_count == 1
    published_payload = event_bus.publish.call_args[1]["event"]
    assert published_payload.content_raw == ""
    assert published_payload.content_clean == ""
    assert published_payload.message_id == "1612345678.000200"


@pytest.mark.asyncio
async def test_slack_listener_threaded_reply() -> None:
    """Test threaded reply populates thread_ts."""
    app = MockAsyncApp()
    engine = create_mock_engine()
    event_bus = AsyncMock()

    register_listeners(app=app, bus=event_bus, db_engine=engine)
    handler = app.listeners["message"]

    event = {
        "type": "message",
        "ts": "1612345682.000300",
        "thread_ts": "1612345678.000200",
        "channel": "C999",
        "team": "T12345",
        "user": "U12345",
        "text": "Thread reply",
    }

    await handler(event)

    assert event_bus.publish.call_count == 1
    published_payload = event_bus.publish.call_args[1]["event"]
    assert published_payload.thread_ts == "1612345678.000200"


@pytest.mark.asyncio
async def test_slack_listener_app_uninstalled() -> None:
    """Test that app_uninstalled deletes the team installation."""
    app = MockAsyncApp()

    mock_conn = AsyncMock()
    engine = MagicMock()
    engine.begin.return_value.__aenter__.return_value = mock_conn
    engine.connect.return_value.__aenter__.return_value = mock_conn

    event_bus = AsyncMock()

    register_listeners(app=app, bus=event_bus, db_engine=engine)
    assert "app_uninstalled" in app.listeners
    handler = app.listeners["app_uninstalled"]

    body = {"team_id": "T123"}
    await handler(body)

    # Should run the delete statement inside async_delete_all
    statements = data_statements(mock_conn)
    assert len(statements) == 1
    assert str(statements[0]).startswith("DELETE")


@pytest.mark.asyncio
async def test_slack_listener_tokens_revoked() -> None:
    """Test that tokens_revoked deletes the team installation."""
    app = MockAsyncApp()

    mock_conn = AsyncMock()
    engine = MagicMock()
    engine.begin.return_value.__aenter__.return_value = mock_conn
    engine.connect.return_value.__aenter__.return_value = mock_conn

    event_bus = AsyncMock()

    register_listeners(app=app, bus=event_bus, db_engine=engine)
    assert "tokens_revoked" in app.listeners
    handler = app.listeners["tokens_revoked"]

    body = {"team_id": "T123"}
    event = {"tokens": {"oauth": ["U123"]}}
    await handler(body, event)

    # Should run the delete statement inside async_delete_all
    statements = data_statements(mock_conn)
    assert len(statements) == 1
    assert str(statements[0]).startswith("DELETE")


@pytest.mark.asyncio
async def test_slack_listener_bot_filtering() -> None:
    """Verify bot noise filtering in Slack listener."""
    app = MockAsyncApp()
    engine = create_mock_engine()
    event_bus = AsyncMock()

    register_listeners(app=app, bus=event_bus, db_engine=engine)
    handler = app.listeners["message"]

    # 1. Filtered by blocked bot_id
    event_bot = {
        "type": "message",
        "ts": "1612345678.000200",
        "channel": "C999",
        "team": "T12345",
        "bot_id": "USLACKBOT",
        "text": "Hello world",
    }
    await handler(event_bot)
    assert event_bus.publish.call_count == 0

    # 2. Filtered by blocked username
    event_user = {
        "type": "message",
        "ts": "1612345678.000200",
        "channel": "C999",
        "team": "T12345",
        "username": "datadog",
        "text": "System metrics normal",
    }
    await handler(event_user)
    assert event_bus.publish.call_count == 0

    # 3. Filtered by blocked content pattern
    event_text = {
        "type": "message",
        "ts": "1612345678.000200",
        "channel": "C999",
        "team": "T12345",
        "user": "U12345",
        "text": "[sentry] ZeroDivisionError: division by zero",
    }
    await handler(event_text)
    assert event_bus.publish.call_count == 0

    # 4. Message deleted subtype with blocked features is NOT filtered
    event_deleted = {
        "type": "message",
        "subtype": "message_deleted",
        "ts": "1612345688.000400",
        "channel": "C999",
        "team": "T12345",
        "deleted_ts": "1612345678.000200",
        "bot_id": "USLACKBOT",
        "previous_message": {
            "type": "message",
            "user": "U12345",
            "text": "[sentry] Original error",
            "ts": "1612345678.000200",
        },
    }
    await handler(event_deleted)
    assert event_bus.publish.call_count == 1


@pytest.mark.asyncio
async def test_slack_private_channel_acl() -> None:
    """Verify private channel detection, client calls, caching, and ACL population."""
    from unittest.mock import AsyncMock, patch

    app = MockAsyncApp()
    engine = create_mock_engine()
    event_bus = AsyncMock()

    # Setup fake Redis mock
    fake_redis = AsyncMock()
    # Initially cache is empty (returns None)
    fake_redis.get.return_value = None
    event_bus.redis = fake_redis

    register_listeners(app=app, bus=event_bus, db_engine=engine)
    handler = app.listeners["message"]

    # Mock Slack Client
    mock_client = AsyncMock()
    mock_client.conversations_info.return_value = {"ok": True, "channel": {"is_private": True}}
    mock_client.conversations_members.return_value = {
        "ok": True,
        "members": ["U111", "U222"],
        "response_metadata": {},
    }

    event = {
        "type": "message",
        "ts": "1612345678.000200",
        "channel": "C999",
        "team": "T12345",
        "user": "U12345",
        "text": "Private msg",
    }

    tenant_id = UUID("11111111-1111-1111-1111-111111111111")
    resolved_ids = [
        UUID("22222222-2222-2222-2222-222222222222"),
        UUID("33333333-3333-3333-3333-333333333333"),
    ]

    matched = (dict(zip(["U111", "U222"], resolved_ids, strict=True)), [])
    with patch(
        "checkcord.services.accounts.match", AsyncMock(return_value=matched)
    ) as mock_resolve:
        await handler(event, client=mock_client)

        # Verify conversations_info and conversations_members were called
        mock_client.conversations_info.assert_called_once_with(channel="C999")
        mock_client.conversations_members.assert_called_once_with(channel="C999", limit=100)

        _, tenant, platform, native_ids, _ = mock_resolve.call_args.args
        assert (tenant, platform, list(native_ids)) == (tenant_id, "slack", ["U111", "U222"])

        # Verify event RawChatPayload fields
        assert event_bus.publish.call_count == 1
        published_event = event_bus.publish.call_args[1]["event"]
        assert published_event.channel_id == "C999"


@pytest.mark.asyncio
async def test_slack_public_channel_and_prefixes() -> None:
    """Verify starts with G/D is private, and C is public if conversations_info returns False."""
    from unittest.mock import AsyncMock

    app = MockAsyncApp()
    engine = create_mock_engine()
    event_bus = AsyncMock()
    fake_redis = AsyncMock()
    fake_redis.get.return_value = None
    event_bus.redis = fake_redis

    register_listeners(app=app, bus=event_bus, db_engine=engine)
    handler = app.listeners["message"]

    # 1. Public channel test
    mock_client = AsyncMock()
    mock_client.conversations_info.return_value = {"ok": True, "channel": {"is_private": False}}

    event_public = {
        "type": "message",
        "ts": "1612345678.000200",
        "channel": "C123",
        "team": "T12345",
        "user": "U12345",
        "text": "Public msg",
    }

    await handler(event_public, client=mock_client)
    published_event = event_bus.publish.call_args[1]["event"]
    assert published_event.channel_id == "C123"

    # 2. Prefixes G / D test
    event_bus.publish.reset_mock()
    mock_client.reset_mock()

    event_g = {
        "type": "message",
        "ts": "1612345678.000200",
        "channel": "G123",
        "team": "T12345",
        "user": "U12345",
        "text": "Private Group msg",
    }

    mock_client.conversations_members.return_value = {
        "ok": True,
        "members": ["U111"],
        "response_metadata": {},
    }

    await handler(event_g, client=mock_client)
    # conversations_info should NOT be called for G channels
    mock_client.conversations_info.assert_not_called()
    mock_client.conversations_members.assert_called_once_with(channel="G123", limit=100)

    published_event_g = event_bus.publish.call_args[1]["event"]
    assert published_event_g.channel_id == "G123"


@pytest.mark.asyncio
async def test_slack_attachment_downloads() -> None:
    """Verify text attachment downloading, size limit (5MB), and failure handling."""
    from unittest.mock import AsyncMock, patch

    app = MockAsyncApp()
    engine = create_mock_engine()
    event_bus = AsyncMock()
    # Disable redis mock return values to simplify test
    event_bus.redis = None

    register_listeners(app=app, bus=event_bus, db_engine=engine)
    handler = app.listeners["message"]

    # Mock Slack Client (public channel to avoid member lookup overhead)
    mock_client = AsyncMock()
    mock_client.conversations_info.return_value = {"ok": True, "channel": {"is_private": False}}

    event = {
        "type": "message",
        "ts": "1612345678.000200",
        "channel": "C999",
        "team": "T12345",
        "user": "U12345",
        "text": "Check files",
        "files": [
            {
                "name": "doc.txt",
                "mimetype": "text/plain",
                "size": 100,
                "url_private": "https://example.com/doc.txt",
            },
            {
                "name": "big.txt",
                "mimetype": "text/plain",
                "size": 6 * 1024 * 1024,  # 6MB
                "url_private": "https://example.com/big.txt",
            },
            {
                "name": "image.png",
                "mimetype": "image/png",
                "size": 100,
                "url_private": "https://example.com/image.png",
            },
            {
                "name": "fail.json",
                "mimetype": "application/json",
                "size": 200,
                "url_private": "https://example.com/fail.json",
            },
        ],
    }

    # Mock aiohttp responses
    class MockAiohttpResponse:
        def __init__(self, text_content: str, status: int = 200) -> None:
            self.text_content = text_content
            self.status = status

        async def __aenter__(self) -> "MockAiohttpResponse":
            return self

        async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
            pass

        async def text(self) -> str:
            return self.text_content

    class MockAiohttpClientSession:
        async def __aenter__(self) -> "MockAiohttpClientSession":
            return self

        async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
            pass

        def get(self, url: str, headers: Any = None, **kwargs: Any) -> MockAiohttpResponse:
            if "doc.txt" in url:
                return MockAiohttpResponse("Hello from doc.txt")
            elif "fail.json" in url:
                return MockAiohttpResponse("Failed download", status=500)
            raise RuntimeError("Unexpected URL")

    with patch("aiohttp.ClientSession", return_value=MockAiohttpClientSession()):
        await handler(event, client=mock_client)

        # Verify publication happened
        assert event_bus.publish.call_count == 1
        published_event = event_bus.publish.call_args[1]["event"]

        # Check content_raw contains doc.txt content
        assert "=== Attachment: doc.txt ===" in published_event.content_raw
        assert "Hello from doc.txt" in published_event.content_raw

        # Check warning for fail.json
        assert "[Warning: Failed to download attachment fail.json]" in published_event.content_raw

        # Check big.txt (exceeds 5MB) was NOT downloaded
        assert "big.txt" not in published_event.content_raw

        # Check image.png (not text mimetype or extension) was NOT downloaded
        assert "image.png" not in published_event.content_raw
