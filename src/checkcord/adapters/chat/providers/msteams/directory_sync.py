"""Sync Teams users, teams and channels into the directory tables."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import aiohttp
import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.core import http_retry
from checkcord.core.config import get_settings
from checkcord.models.enums import SourcePlatform
from checkcord.services import accounts
from checkcord.storage.models import ChatChannel, ChatGroup, ChatUser, MSTeamsInstallation
from checkcord.storage.postgres import app_bypass_rls_context, tenant_context

logger = structlog.get_logger(__name__)


async def get_msteams_access_token(
    client_id: str, client_secret: str, tenant_id: str
) -> str | None:
    """Retrieve an access token for Microsoft Graph API using client credentials flow."""
    settings = get_settings()
    url = f"{settings.ms_login_url}/{tenant_id}/oauth2/v2.0/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": settings.graph_scope,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return str(data.get("access_token"))
                else:
                    logger.error(
                        "msteams.graph_token.failed",
                        status=resp.status,
                        body=await resp.text(),
                    )
    except Exception as e:
        logger.exception("msteams.graph_token.error", error=str(e))
    return None


_KEY = ["tenant_id", "platform", "native_id"]


async def _graph_list(session: aiohttp.ClientSession, url: str) -> tuple[list[dict[str, Any]], int]:
    """Every page of a Graph listing, with the status of the last response."""
    items: list[dict[str, Any]] = []
    next_url: str | None = url
    limited = 0
    while next_url:
        async with session.get(next_url) as resp:
            if resp.status == 429:
                limited += 1
                if await http_retry.wait_or_give_up(
                    "microsoft", limited, resp.headers.get("Retry-After")
                ):
                    continue
            if resp.status != 200:
                return items, resp.status
            data = await resp.json()
        limited = 0
        items.extend(data.get("value", []))
        next_url = data.get("@odata.nextLink")
    return items, 200


async def _upsert(
    db_engine: AsyncEngine,
    tenant_id: UUID,
    model: Any,
    values: dict[str, Any],
    *,
    merge_metadata: bool = False,
) -> None:
    stmt = pg_insert(model).values(
        tenant_id=tenant_id, platform=SourcePlatform.MS_TEAMS.value, **values
    )
    updates = {key: stmt.excluded[key] for key in values if key not in ("native_id", "metadata_")}
    # Merge, so keys set elsewhere survive a sync.
    updates["metadata"] = (
        model.metadata_.op("||")(stmt.excluded.metadata)
        if merge_metadata
        else stmt.excluded.metadata
    )
    updates["updated_at"] = func.now()
    async with tenant_context(db_engine, tenant_id) as conn:
        await conn.execute(stmt.on_conflict_do_update(index_elements=_KEY, set_=updates))


async def sync_msteams_directory(
    db_engine: AsyncEngine, tenant_id: UUID | None = None, redis: Any = None
) -> None:
    """Synchronize users, teams, and channels from Microsoft Teams to the database, then match
    the users to people. ``tenant_id`` limits it to one tenant's installations."""
    settings = get_settings()
    if not settings.msteams_client_id or not settings.msteams_client_secret:
        logger.warning("msteams.directory_sync.not_configured")
        return

    logger.info("msteams.directory_sync.started")

    async with app_bypass_rls_context(db_engine) as conn:
        stmt = select(MSTeamsInstallation.ms_tenant_id, MSTeamsInstallation.tenant_id)
        if tenant_id is not None:
            stmt = stmt.where(MSTeamsInstallation.tenant_id == tenant_id)
        result = await conn.execute(stmt)
        installations = result.fetchall()

    for inst in installations:
        await _sync_installation(db_engine, inst.ms_tenant_id, inst.tenant_id)
        await accounts.after_sync(db_engine, inst.tenant_id, SourcePlatform.MS_TEAMS.value, redis)


async def after_install(db_engine: AsyncEngine, tenant_id: UUID, redis: Any = None) -> None:
    try:
        await sync_msteams_directory(db_engine, tenant_id, redis)
    except Exception as e:
        logger.warning(
            "msteams_directory.install_sync_failed", tenant_id=str(tenant_id), error=str(e)
        )


async def run_scheduled(engine: AsyncEngine, bus: Any) -> int:
    await sync_msteams_directory(engine, redis=getattr(bus, "redis", None))
    return 0


async def _sync_installation(db_engine: AsyncEngine, ms_tenant_id: str, tenant_id: UUID) -> None:
    settings = get_settings()
    graph = settings.graph_api
    client_id = settings.msteams_client_id or ""
    client_secret = settings.msteams_client_secret or ""

    token = await get_msteams_access_token(client_id, client_secret, ms_tenant_id)
    if not token:
        logger.error("msteams.directory_sync.no_token", ms_tenant_id=ms_tenant_id)
        return

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            users, status = await _graph_list(session, f"{graph}/users")
        if status != 200:
            logger.error("msteams.directory_sync.users_api_error", status=status)
        else:
            logger.info("msteams.directory_sync.users", count=len(users), tenant_id=str(tenant_id))
            for user in users:
                await _upsert(
                    db_engine,
                    tenant_id,
                    ChatUser,
                    {
                        "native_id": user["id"],
                        "display_name": user.get("displayName")
                        or user.get("userPrincipalName")
                        or user["id"],
                        "email": user.get("mail") or user.get("userPrincipalName"),
                        "metadata_": user,
                    },
                )
    except Exception as e:
        logger.exception(
            "msteams.directory_sync.users_failed", error=str(e), tenant_id=str(tenant_id)
        )

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            teams, status = await _graph_list(
                session,
                f"{graph}/groups?$filter=resourceProvisioningOptions/any(x:x eq 'Team')",
            )
            if status != 200:
                teams, status = await _graph_list(session, f"{graph}/groups")
            if status != 200:
                logger.error("msteams.directory_sync.teams_api_error", status=status)
                return

            logger.info("msteams.directory_sync.groups", count=len(teams), tenant_id=str(tenant_id))
            for team in teams:
                team_id = team["id"]
                await _upsert(
                    db_engine,
                    tenant_id,
                    ChatGroup,
                    {
                        "native_id": team_id,
                        "display_name": team.get("displayName") or team_id,
                        "metadata_": team,
                    },
                )
                try:
                    channels, status = await _graph_list(
                        session, f"{graph}/teams/{team_id}/channels"
                    )
                    if status != 200:
                        logger.error(
                            "msteams.directory_sync.channels_api_error",
                            status=status,
                            team_id=team_id,
                        )
                        continue
                    for channel in channels:
                        channel["team_id"] = team_id
                        await _upsert(
                            db_engine,
                            tenant_id,
                            ChatChannel,
                            {
                                "native_id": channel["id"],
                                "display_name": channel.get("displayName") or channel["id"],
                                "metadata_": channel,
                            },
                            merge_metadata=True,
                        )
                except Exception as e:
                    logger.exception(
                        "msteams.directory_sync.channels_failed",
                        team_id=team_id,
                        error=str(e),
                    )
    except Exception as e:
        logger.exception(
            "msteams.directory_sync.groups_failed", error=str(e), tenant_id=str(tenant_id)
        )
