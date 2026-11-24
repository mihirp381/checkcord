"""Integration tests for the resolve_author_identity utility."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.adapters.chat.core.identity import resolve_author_identity
from checkcord.adapters.chat.providers.slack.listeners import register_listeners
from checkcord.core.config import get_settings

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


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


async def test_resolve_author_identity_lifecycle() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    tenant_id = uuid4()
    other_tenant_id = uuid4()

    # Generate unique emails and identifiers
    email_resolved = f"resolved_{uuid4().hex[:6]}@example.com"
    native_resolved = f"N_{uuid4().hex[:6]}"
    native_unresolved = f"N_{uuid4().hex[:6]}"
    native_no_email = f"N_{uuid4().hex[:6]}"

    ent_user_id = uuid4()
    chat_user_resolved_id = uuid4()
    chat_user_unresolved_id = uuid4()
    chat_user_no_email_id = uuid4()

    # 1. Insert test data
    async with engine.begin() as conn:
        # Create enterprise user
        await conn.execute(
            text(
                "INSERT INTO enterprise_users (id, tenant_id, primary_email, display_name, active) "
                "VALUES (:id, :tenant_id, :email, 'Enterprise User', True)"
            ),
            {"id": ent_user_id, "tenant_id": tenant_id, "email": email_resolved},
        )

        # Create chat user that already has internal_id linked
        await conn.execute(
            text(
                "INSERT INTO chat_users "
                "(id, tenant_id, platform, native_id, display_name, email, internal_id) "
                "VALUES (:id, :tenant_id, 'slack', :native_id, 'Chat Resolved', :email, "
                ":internal_id)"
            ),
            {
                "id": chat_user_resolved_id,
                "tenant_id": tenant_id,
                "native_id": native_resolved,
                "email": email_resolved,
                "internal_id": ent_user_id,
            },
        )

        # Create chat user that has email but internal_id is None (needs to be resolved)
        await conn.execute(
            text(
                "INSERT INTO chat_users "
                "(id, tenant_id, platform, native_id, display_name, email, internal_id) "
                "VALUES (:id, :tenant_id, 'slack', :native_id, 'Chat Unresolved', :email, "
                "NULL)"
            ),
            {
                "id": chat_user_unresolved_id,
                "tenant_id": tenant_id,
                "native_id": native_unresolved,
                "email": email_resolved,
            },
        )

        # Create chat user that has no email and internal_id is None
        await conn.execute(
            text(
                "INSERT INTO chat_users "
                "(id, tenant_id, platform, native_id, display_name, email, internal_id) "
                "VALUES (:id, :tenant_id, 'slack', :native_id, 'Chat No Email', NULL, "
                "NULL)"
            ),
            {
                "id": chat_user_no_email_id,
                "tenant_id": tenant_id,
                "native_id": native_no_email,
            },
        )

    try:
        # Test Case 1: ChatUser does not exist
        u_id, email, name = await resolve_author_identity(
            db_engine=engine,
            tenant_id=tenant_id,
            platform="slack",
            native_id="non_existent_native_id",
        )
        assert u_id is None
        assert email is None
        assert name is None

        # Test Case 2: ChatUser exists with internal_id set
        u_id, email, name = await resolve_author_identity(
            db_engine=engine,
            tenant_id=tenant_id,
            platform="slack",
            native_id=native_resolved,
        )
        assert u_id == ent_user_id
        assert email == email_resolved
        assert name == "Chat Resolved"

        # Test Case 3: ChatUser has no internal_id, but has email -> queries and links
        u_id, email, name = await resolve_author_identity(
            db_engine=engine,
            tenant_id=tenant_id,
            platform="slack",
            native_id=native_unresolved,
        )
        assert u_id == ent_user_id
        assert email == email_resolved
        assert name == "Chat Unresolved"

        # Verify that internal_id was updated in database
        async with engine.begin() as conn:
            res = await conn.execute(
                text("SELECT internal_id FROM chat_users WHERE id = :id"),
                {"id": chat_user_unresolved_id},
            )
            linked_id = res.scalar()
            assert linked_id == ent_user_id

        # Test Case 4: ChatUser has no internal_id, no email
        u_id, email, name = await resolve_author_identity(
            db_engine=engine,
            tenant_id=tenant_id,
            platform="slack",
            native_id=native_no_email,
        )
        assert u_id is None
        assert email is None
        assert name == "Chat No Email"

        # Test Case 5: Tenant isolation
        u_id, email, name = await resolve_author_identity(
            db_engine=engine,
            tenant_id=other_tenant_id,
            platform="slack",
            native_id=native_resolved,
        )
        assert u_id is None
        assert email is None
        assert name is None

    finally:
        # Cleanup
        async with engine.begin() as conn:
            # Bypass RLS to clean up
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM chat_users WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
            await conn.execute(
                text("DELETE FROM enterprise_users WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
        await engine.dispose()


async def test_resolve_author_identity_stamped_on_event() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    tenant_id = uuid4()
    team_id = f"T_{uuid4().hex[:6]}"
    user_id = f"U_{uuid4().hex[:6]}"
    email = f"user_{uuid4().hex[:6]}@example.com"

    ent_user_id = uuid4()
    chat_user_id = uuid4()

    # Setup database records
    async with engine.begin() as conn:
        # Create Slack Installation
        await conn.execute(
            text(
                "INSERT INTO slack_installations "
                "(id, team_id, tenant_id, bot_token, installation_info) "
                "VALUES (:id, :team_id, :tenant_id, 'xoxb-dummy', '{}')"
            ),
            {"id": uuid4(), "team_id": team_id, "tenant_id": tenant_id},
        )
        # Create Enterprise User
        await conn.execute(
            text(
                "INSERT INTO enterprise_users (id, tenant_id, primary_email, display_name, active) "
                "VALUES (:id, :tenant_id, :email, 'Alice Enterprise', True)"
            ),
            {"id": ent_user_id, "tenant_id": tenant_id, "email": email},
        )
        # Create Chat User linked to Enterprise User
        await conn.execute(
            text(
                "INSERT INTO chat_users "
                "(id, tenant_id, platform, native_id, display_name, email, internal_id) "
                "VALUES (:id, :tenant_id, 'slack', :native_id, 'Alice Slack', :email, "
                ":internal_id)"
            ),
            {
                "id": chat_user_id,
                "tenant_id": tenant_id,
                "native_id": user_id,
                "email": email,
                "internal_id": ent_user_id,
            },
        )

    try:
        app = MockAsyncApp()
        event_bus = AsyncMock()

        # Register listeners using the real database engine
        register_listeners(app=app, bus=event_bus, db_engine=engine)
        handler = app.listeners["message"]

        event = {
            "type": "message",
            "ts": "1612345678.000200",
            "channel": "C123",
            "team": team_id,
            "user": user_id,
            "text": "Hello world",
        }

        # Process the Slack message event
        await handler(event)

        # Assert event_bus.publish was called
        assert event_bus.publish.call_count == 1
        published_args = event_bus.publish.call_args[1]
        published_event = published_args["event"]

        # Verify that the author info is resolved and stamped
        assert published_event.author_id == user_id
        assert published_event.author_email == email
        assert published_event.author_name == "Alice Slack"

    finally:
        # Cleanup
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM chat_users WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
            await conn.execute(
                text("DELETE FROM enterprise_users WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
            await conn.execute(
                text("DELETE FROM slack_installations WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
        await engine.dispose()
