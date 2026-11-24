"""Who can read a Slack channel."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.models.common import AccessControl
from checkcord.services import accounts, audiences

logger = structlog.get_logger(__name__)


async def read_slack_channel(
    client: Any, channel_id: str, is_private: bool | None = None
) -> audiences.Reading:
    """Visibility and member ids of a channel; raises ``ReadFailed`` when either is unreadable.

    Only a channel Slack confirms as public counts as public.
    """
    if is_private is None:
        is_private = True
        if channel_id.startswith("C"):
            resp = await client.conversations_info(channel=channel_id)
            if not resp.get("ok"):
                raise audiences.ReadFailed(f"conversations.info: {resp.get('error')}")
            is_private = resp.get("channel", {}).get("is_private") is not False
    if not is_private:
        return audiences.Reading(is_public=True)

    members: list[str] = []
    cursor = None
    while True:
        kwargs: dict[str, Any] = {"channel": channel_id, "limit": 100}
        if cursor:
            kwargs["cursor"] = cursor
        resp = await client.conversations_members(**kwargs)
        if not resp.get("ok"):
            raise audiences.ReadFailed(f"conversations.members: {resp.get('error')}")
        members.extend(str(m) for m in resp.get("members", []))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    if not members:
        raise audiences.ReadFailed("conversations.members returned no members")
    return audiences.Reading(
        is_public=False, native_ids=tuple(members), lookup=lambda ids: lookup_users(client, ids)
    )


def profile_of(member: dict[str, Any]) -> accounts.Profile:
    profile = member.get("profile") or {}
    return accounts.Profile(
        native_id=str(member["id"]),
        display_name=profile.get("display_name") or member.get("real_name") or None,
        email=profile.get("email"),
        metadata=member,
    )


async def lookup_users(client: Any, user_ids: list[str]) -> list[accounts.Profile]:
    """``users.info`` for each id; ids Slack does not return are skipped."""
    found = []
    for user_id in user_ids:
        resp = await client.users_info(user=user_id)
        if resp.get("ok") and resp.get("user") and not resp["user"].get("deleted"):
            found.append(profile_of(resp["user"]))
    return found


async def resolve_slack_audience(
    *,
    client: Any,
    redis: Redis | None,
    db_engine: AsyncEngine,
    tenant_id: UUID,
    channel_id: str,
    is_private: bool | None = None,
    team_id: str | None = None,
) -> AccessControl:
    """Public for a public channel, otherwise the members Checkcord can map to its users."""
    return await audiences.current(
        db_engine,
        tenant_id,
        audiences.SLACK,
        channel_id,
        reader=lambda: read_slack_channel(client, channel_id, True if is_private else None),
        details={"team_id": team_id} if team_id else None,
        is_public_hint=False if is_private else None,
        redis=redis,
    )
