"""Sync Slack users, channels and user groups into the directory tables."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.core import http_retry
from checkcord.core.config import get_settings
from checkcord.core.crypto import decrypt_secret
from checkcord.models.enums import SourcePlatform
from checkcord.services import accounts
from checkcord.storage.models import ChatChannel, ChatGroup, ChatUser, SlackInstallation
from checkcord.storage.postgres import app_bypass_rls_context, tenant_context

logger = structlog.get_logger(__name__)


async def _list_all(call: Any, key: str, **kwargs: Any) -> list[dict[str, Any]] | None:
    """Every page of a cursor-paginated Slack listing; None if Slack refuses a page."""
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        res = await call(cursor=cursor, limit=200, **kwargs)
        if not res.get("ok"):
            logger.error("slack.directory_sync.list_not_ok", listing=key, error=res.get("error"))
            return None
        items.extend(res.get(key, []))
        cursor = (res.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return items


async def sync_slack_directory(
    db_engine: AsyncEngine, tenant_id: UUID | None = None, redis: Any = None
) -> None:
    """Synchronize users, channels, and groups from Slack to the database, then match the
    users to people. ``tenant_id`` limits it to one tenant's installations."""
    logger.info("slack.directory_sync.started")

    async with app_bypass_rls_context(db_engine) as conn:
        stmt = select(
            SlackInstallation.bot_token,
            SlackInstallation.tenant_id,
        )
        if tenant_id is not None:
            stmt = stmt.where(SlackInstallation.tenant_id == tenant_id)
        result = await conn.execute(stmt)
        installations = result.fetchall()

    for inst in installations:
        await _sync_installation(db_engine, decrypt_secret(inst.bot_token), inst.tenant_id)
        await accounts.after_sync(db_engine, inst.tenant_id, SourcePlatform.SLACK.value, redis)


async def after_install(db_engine: AsyncEngine, tenant_id: UUID, redis: Any = None) -> None:
    try:
        await sync_slack_directory(db_engine, tenant_id, redis)
    except Exception as e:
        logger.warning(
            "slack_directory.install_sync_failed", tenant_id=str(tenant_id), error=str(e)
        )


async def run_scheduled(engine: AsyncEngine, bus: Any) -> int:
    await sync_slack_directory(engine, redis=getattr(bus, "redis", None))
    return 0


async def _sync_installation(db_engine: AsyncEngine, bot_token: str, tenant_id: UUID) -> None:
    client = http_retry.bounded_slack(
        AsyncWebClient(token=bot_token, base_url=get_settings().slack_api_url)
    )

    # Sync Users
    try:
        members = await _list_all(client.users_list, "members")
        if members is not None:
            logger.info("slack.directory_sync.users", count=len(members), tenant_id=str(tenant_id))
            for member in members:
                if member.get("deleted"):
                    continue
                native_id = member["id"]
                profile = member.get("profile", {})
                display_name = profile.get("display_name") or member.get("real_name") or native_id
                email = profile.get("email")

                async with tenant_context(db_engine, tenant_id) as conn:
                    stmt_user = pg_insert(ChatUser).values(
                        tenant_id=tenant_id,
                        platform=SourcePlatform.SLACK.value,
                        native_id=native_id,
                        display_name=display_name,
                        email=email,
                        metadata_=member,
                    )
                    stmt_user = stmt_user.on_conflict_do_update(
                        index_elements=["tenant_id", "platform", "native_id"],
                        set_={
                            "display_name": stmt_user.excluded.display_name,
                            "email": stmt_user.excluded.email,
                            "metadata": stmt_user.excluded.metadata,
                            "updated_at": func.now(),
                        },
                    )
                    await conn.execute(stmt_user)
    except Exception as e:
        logger.exception(
            "slack.directory_sync.users_failed", error=str(e), tenant_id=str(tenant_id)
        )

    # Sync Channels
    try:
        channels = await _list_all(
            client.conversations_list, "channels", types="public_channel,private_channel"
        )
        if channels is not None:
            logger.info(
                "slack.directory_sync.channels", count=len(channels), tenant_id=str(tenant_id)
            )
            for channel in channels:
                native_id = channel["id"]
                display_name = channel.get("name") or native_id

                async with tenant_context(db_engine, tenant_id) as conn:
                    stmt_chan = pg_insert(ChatChannel).values(
                        tenant_id=tenant_id,
                        platform=SourcePlatform.SLACK.value,
                        native_id=native_id,
                        display_name=display_name,
                        metadata_=channel,
                    )
                    stmt_chan = stmt_chan.on_conflict_do_update(
                        index_elements=["tenant_id", "platform", "native_id"],
                        set_={
                            "display_name": stmt_chan.excluded.display_name,
                            # Merge, so keys set elsewhere survive a sync.
                            "metadata": ChatChannel.metadata_.op("||")(stmt_chan.excluded.metadata),
                            "updated_at": func.now(),
                        },
                    )
                    await conn.execute(stmt_chan)
    except Exception as e:
        logger.exception(
            "slack.directory_sync.channels_failed", error=str(e), tenant_id=str(tenant_id)
        )

    # Sync User Groups
    try:
        res_groups = await client.usergroups_list()
        if res_groups.get("ok"):
            groups: list[dict[str, Any]] = res_groups.get("usergroups", [])
            logger.info(
                "slack.directory_sync.user_groups", count=len(groups), tenant_id=str(tenant_id)
            )
            for group in groups:
                native_id = group["id"]
                display_name = group.get("name") or native_id

                async with tenant_context(db_engine, tenant_id) as conn:
                    stmt_group = pg_insert(ChatGroup).values(
                        tenant_id=tenant_id,
                        platform=SourcePlatform.SLACK.value,
                        native_id=native_id,
                        display_name=display_name,
                        metadata_=group,
                    )
                    stmt_group = stmt_group.on_conflict_do_update(
                        index_elements=["tenant_id", "platform", "native_id"],
                        set_={
                            "display_name": stmt_group.excluded.display_name,
                            "metadata": stmt_group.excluded.metadata,
                            "updated_at": func.now(),
                        },
                    )
                    await conn.execute(stmt_group)
        else:
            logger.error("slack.directory_sync.user_groups_not_ok", error=res_groups.get("error"))
    except Exception as e:
        logger.info(
            "slack.directory_sync.user_groups_failed",
            error=str(e),
            tenant_id=str(tenant_id),
        )
