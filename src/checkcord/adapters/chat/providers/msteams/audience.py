"""Who can read a Microsoft Teams channel or chat."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import aiohttp
import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.chat.providers.msteams import directory_sync
from checkcord.core import http_retry
from checkcord.core.config import get_settings
from checkcord.models.common import AccessControl
from checkcord.models.enums import SourcePlatform
from checkcord.services import accounts, audiences
from checkcord.storage.models import ChatChannel
from checkcord.storage.postgres import tenant_context

logger = structlog.get_logger(__name__)


def channel_id_of(conversation_id: str) -> str:
    """Channel part of a Bot Framework conversation id, without the thread suffix."""
    return conversation_id.split(";messageid=", 1)[0]


class _Graph:
    def __init__(self, ms_tenant_id: str, token: str | None) -> None:
        self.ms_tenant_id = ms_tenant_id
        self.token = token

    async def get(self, path: str) -> dict[str, Any] | None:
        if self.token is None:
            settings = get_settings()
            if not settings.msteams_client_id or not settings.msteams_client_secret:
                return None
            self.token = await directory_sync.get_msteams_access_token(
                settings.msteams_client_id, settings.msteams_client_secret, self.ms_tenant_id
            )
            if not self.token:
                return None
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                attempt = 0
                while True:
                    attempt += 1
                    async with session.get(f"{get_settings().graph_api}/{path}") as resp:
                        if resp.status == 429 and await http_retry.wait_or_give_up(
                            "microsoft", attempt, resp.headers.get("Retry-After")
                        ):
                            continue
                        if resp.status != 200:
                            logger.warning(
                                "msteams_audience.graph_error", path=path, status=resp.status
                            )
                            return None
                        data = await resp.json()
                        return data if isinstance(data, dict) else None
        except Exception as e:
            logger.warning("msteams_audience.graph_failed", path=path, error=str(e))
            return None


async def _team_id_for(db_engine: AsyncEngine, tenant_id: UUID, channel_id: str) -> str | None:
    try:
        async with tenant_context(db_engine, tenant_id) as conn:
            res = await conn.execute(
                select(ChatChannel.metadata_).where(
                    ChatChannel.tenant_id == tenant_id,
                    ChatChannel.platform == SourcePlatform.MS_TEAMS.value,
                    ChatChannel.native_id == channel_id,
                )
            )
            row = res.fetchone()
    except Exception as e:
        logger.warning("msteams_audience.team_lookup_failed", channel_id=channel_id, error=str(e))
        return None
    if row and isinstance(row[0], dict):
        team_id = row[0].get("team_id") or row[0].get("ms_team_id")
        return str(team_id) if team_id else None
    return None


async def read_msteams_conversation(
    *,
    db_engine: AsyncEngine,
    tenant_id: UUID,
    ms_tenant_id: str,
    conversation_type: str | None,
    channel_id: str,
    team_id: str | None = None,
    membership_type: str | None = None,
    token: str | None = None,
) -> audiences.Reading:
    """Public only for a standard team channel; raises ``ReadFailed`` when Graph cannot be read.

    A chat, or a private or shared channel, lists its members. Teams cannot change a
    channel's membership type, so a ``membership_type`` hint is trusted.
    """
    graph = _Graph(ms_tenant_id, token)
    if conversation_type in (None, "channel") and team_id is None:
        team_id = await _team_id_for(db_engine, tenant_id, channel_id)
    if conversation_type is None:
        conversation_type = "channel" if team_id else "groupChat"
    is_chat = conversation_type in ("personal", "groupChat")

    if is_chat:
        path = f"chats/{channel_id}/members"
    else:
        if not team_id:
            raise audiences.ReadFailed("team of the channel is unknown")
        if conversation_type == "channel" and membership_type is None:
            details = await graph.get(f"teams/{team_id}/channels/{channel_id}")
            if not details or not details.get("membershipType"):
                raise audiences.ReadFailed("channel details unavailable")
            membership_type = str(details["membershipType"])
        if conversation_type == "channel" and membership_type == "standard":
            return audiences.Reading(is_public=True)
        path = f"teams/{team_id}/channels/{channel_id}/members"

    members: list[str] = []
    while path:
        data = await graph.get(path)
        if data is None:
            raise audiences.ReadFailed("members unavailable")
        members.extend(str(m["userId"]) for m in data.get("value", []) if m.get("userId"))
        next_link = data.get("@odata.nextLink")
        path = str(next_link).removeprefix(f"{get_settings().graph_api}/") if next_link else ""
    if not members:
        raise audiences.ReadFailed("no members returned")
    return audiences.Reading(
        is_public=False, native_ids=tuple(members), lookup=lambda ids: _lookup(graph, ids)
    )


def profile_of(user: dict[str, Any]) -> accounts.Profile:
    return accounts.Profile(
        native_id=str(user["id"]),
        display_name=user.get("displayName") or user.get("userPrincipalName") or None,
        email=user.get("mail") or user.get("userPrincipalName"),
        metadata=user,
    )


async def _lookup(graph: _Graph, user_ids: list[str]) -> list[accounts.Profile]:
    found = []
    for user_id in user_ids:
        user = await graph.get(f"users/{user_id}")
        if user and user.get("id"):
            found.append(profile_of(user))
    return found


async def resolve_msteams_audience(
    *,
    redis: Redis | None,
    db_engine: AsyncEngine,
    tenant_id: UUID,
    ms_tenant_id: str,
    conversation_type: str,
    channel_id: str,
    team_id: str | None = None,
    membership_type: str | None = None,
    token: str | None = None,
) -> AccessControl:
    """Public only for a standard team channel; otherwise the members Checkcord can map.

    A chat, a private or shared channel, or a channel whose type cannot be read
    is restricted. When its members cannot be read the audience is empty.
    """
    details = {"ms_tenant_id": ms_tenant_id, "conversation_type": conversation_type}
    if team_id:
        details["team_id"] = team_id
    return await audiences.current(
        db_engine,
        tenant_id,
        audiences.MS_TEAMS,
        channel_id,
        reader=lambda: read_msteams_conversation(
            db_engine=db_engine,
            tenant_id=tenant_id,
            ms_tenant_id=ms_tenant_id,
            conversation_type=conversation_type,
            channel_id=channel_id,
            team_id=team_id,
            membership_type=membership_type,
            token=token,
        ),
        details=details,
        is_public_hint=None if membership_type is None else membership_type == "standard",
        redis=redis,
    )
