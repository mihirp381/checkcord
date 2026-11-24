"""Match chat authors to people."""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.services import accounts
from checkcord.storage.models import ChatChannel, ChatUser
from checkcord.storage.postgres import tenant_context

logger = structlog.get_logger(__name__)


async def resolve_author_identity(
    db_engine: AsyncEngine,
    tenant_id: UUID,
    platform: str,
    native_id: str,
) -> tuple[UUID | None, str | None, str | None]:
    """(internal_id, email, display_name) of an account; an unlinked account is first matched
    to an active person by email, ignoring case, unless it was unlinked by hand."""
    async with tenant_context(db_engine, tenant_id) as conn:
        stmt = select(ChatUser).where(
            ChatUser.tenant_id == tenant_id,
            ChatUser.platform == platform,
            ChatUser.native_id == native_id,
        )
        chat_user = (await conn.execute(stmt)).mappings().fetchone()
        if not chat_user:
            return None, None, None
        if (
            chat_user["internal_id"] is None
            and chat_user["email"] is not None
            and chat_user["link_source"] != accounts.MANUAL
        ):
            await accounts.link_by_email(conn, tenant_id, platform)
            chat_user = (await conn.execute(stmt)).mappings().fetchone()
            if chat_user and chat_user["internal_id"] is not None:
                logger.info(
                    "resolve_author_identity.linked_by_email",
                    tenant_id=str(tenant_id),
                    chat_user_id=str(chat_user["id"]),
                    internal_id=str(chat_user["internal_id"]),
                )
        if not chat_user:
            return None, None, None
        return chat_user["internal_id"], chat_user["email"], chat_user["display_name"]


async def resolve_identity_map(
    db_engine: AsyncEngine,
    tenant_id: UUID,
    platform: str,
    native_ids: list[str],
) -> dict[str, UUID]:
    """Map each resolvable native_id to its enterprise internal_id; unresolved ids are absent.

    Unlinked accounts are matched by email first, as in ``resolve_author_identity``.
    """
    if not native_ids:
        return {}
    stmt = select(
        ChatUser.native_id, ChatUser.internal_id, ChatUser.email, ChatUser.link_source
    ).where(
        ChatUser.tenant_id == tenant_id,
        ChatUser.platform == platform,
        ChatUser.native_id.in_(native_ids),
    )
    async with tenant_context(db_engine, tenant_id) as conn:
        rows = (await conn.execute(stmt)).all()
        if any(
            r.internal_id is None and r.email is not None and r.link_source != accounts.MANUAL
            for r in rows
        ):
            await accounts.link_by_email(conn, tenant_id, platform)
            rows = (await conn.execute(stmt)).all()
    return {r.native_id: r.internal_id for r in rows if r.internal_id is not None}


async def resolve_channel_name(
    db_engine: AsyncEngine,
    tenant_id: UUID,
    platform: str,
    native_id: str,
) -> str | None:
    """Display name of a synced channel, or None when it has not been synced."""
    try:
        async with tenant_context(db_engine, tenant_id) as conn:
            res = await conn.execute(
                select(ChatChannel.display_name).where(
                    ChatChannel.tenant_id == tenant_id,
                    ChatChannel.platform == platform,
                    ChatChannel.native_id == native_id,
                )
            )
            return res.scalar_one_or_none()
    except Exception as e:
        logger.warning("resolve_channel_name.failed", native_id=native_id, error=str(e))
        return None
