"""API keys: only the SHA-256 of a key is stored; the key itself is shown once."""

import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.core.crypto import hash_api_key
from checkcord.storage import audit
from checkcord.storage.models import APIKey, EnterpriseUser
from checkcord.storage.postgres import tenant_context

TOKEN_PREFIX = "chk_"


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


@dataclass(frozen=True)
class KeyInfo:
    id: UUID
    name: str
    is_admin: bool
    active: bool
    created_at: datetime
    user_email: str | None = None
    user_id: UUID | None = None


class AdminKeyExistsError(Exception):
    pass


class UserNotFoundError(Exception):
    pass


async def create_key(
    engine: AsyncEngine,
    tenant_id: UUID,
    name: str,
    admin: bool,
    user_email: str | None = None,
    create_user: bool = False,
    action: str = "key.created",
    actor: audit.Actor = audit.CLI_ACTOR,
) -> tuple[UUID, str]:
    token = generate_token()
    key_id = uuid4()
    async with tenant_context(engine, tenant_id) as conn:
        user_id = None
        owner_email = None
        if user_email is not None:
            email = user_email.strip()
            user_id = await conn.scalar(
                select(EnterpriseUser.id).where(
                    EnterpriseUser.tenant_id == tenant_id,
                    func.lower(EnterpriseUser.primary_email) == email.lower(),
                    EnterpriseUser.active.is_(True),
                )
            )
            if user_id is not None:
                owner_email = await conn.scalar(
                    select(EnterpriseUser.primary_email).where(EnterpriseUser.id == user_id)
                )
            else:
                exists = await conn.scalar(
                    select(EnterpriseUser.id).where(
                        EnterpriseUser.tenant_id == tenant_id,
                        func.lower(EnterpriseUser.primary_email) == email.lower(),
                    )
                )
                if exists is not None or not create_user:
                    raise UserNotFoundError(email)
                owner_email = email
                user_id = uuid4()
                await conn.execute(
                    EnterpriseUser.__table__.insert().values(
                        id=user_id,
                        tenant_id=tenant_id,
                        primary_email=email,
                        display_name=email,
                        active=True,
                    )
                )
                await audit.record(
                    conn,
                    tenant_id,
                    actor,
                    "user.created",
                    params={"email": email, "active": True},
                    record_ids=[user_id],
                )
        await conn.execute(
            APIKey.__table__.insert().values(
                id=key_id,
                hashed_token=hash_api_key(token),
                tenant_id=tenant_id,
                name=name,
                active=True,
                is_admin=admin,
                user_id=user_id,
            )
        )
        await audit.record(
            conn,
            tenant_id,
            actor,
            action,
            params={
                "name": name,
                "admin": admin,
                "user_id": user_id,
                "user_email": owner_email,
            },
            record_ids=[key_id],
        )
    return key_id, token


async def list_keys(
    engine: AsyncEngine, tenant_id: UUID, user_id: UUID | None = None
) -> list[KeyInfo]:
    stmt = (
        select(
            APIKey.id,
            APIKey.name,
            APIKey.is_admin,
            APIKey.active,
            APIKey.created_at,
            EnterpriseUser.primary_email,
            APIKey.user_id,
        )
        .outerjoin(EnterpriseUser, EnterpriseUser.id == APIKey.user_id)
        .where(APIKey.tenant_id == tenant_id)
        .order_by(APIKey.created_at)
    )
    if user_id is not None:
        stmt = stmt.where(APIKey.user_id == user_id)
    async with tenant_context(engine, tenant_id) as conn:
        rows = await conn.execute(stmt)
        return [KeyInfo(*row) for row in rows.fetchall()]


async def revoke_key(
    engine: AsyncEngine, tenant_id: UUID, key_id: UUID, actor: audit.Actor = audit.CLI_ACTOR
) -> bool:
    async with tenant_context(engine, tenant_id) as conn:
        owner = (
            await conn.execute(
                update(APIKey)
                .where(APIKey.id == key_id, APIKey.tenant_id == tenant_id)
                .values(active=False, updated_at=func.now())
                .returning(APIKey.user_id)
            )
        ).first()
        if owner is not None:
            email = (
                await conn.scalar(
                    select(EnterpriseUser.primary_email).where(EnterpriseUser.id == owner[0])
                )
                if owner[0]
                else None
            )
            await audit.record(
                conn,
                tenant_id,
                actor,
                "key.revoked",
                params={"user_id": owner[0], "user_email": email},
                record_ids=[key_id],
            )
        return owner is not None


async def count_active_admin_keys(engine: AsyncEngine, tenant_id: UUID) -> int:
    async with tenant_context(engine, tenant_id) as conn:
        count = await conn.scalar(
            select(func.count())
            .select_from(APIKey)
            .where(APIKey.tenant_id == tenant_id, APIKey.active, APIKey.is_admin)
        )
        return int(count or 0)


async def init_admin_key(engine: AsyncEngine, tenant_id: UUID, force: bool) -> tuple[UUID, str]:
    if not force and await count_active_admin_keys(engine, tenant_id):
        raise AdminKeyExistsError(str(tenant_id))
    return await create_key(engine, tenant_id, "Initial admin key", admin=True, action="key.init")
