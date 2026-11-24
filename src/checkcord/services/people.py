"""People of a tenant: listing, the admin flag and deactivation."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from checkcord.services import sessions
from checkcord.storage import audit
from checkcord.storage.models import EnterpriseUser
from checkcord.storage.postgres import tenant_context

_u = EnterpriseUser.__table__.c
FIELDS = (_u.id, _u.primary_email, _u.display_name, _u.active, _u.is_admin, _u.created_at)


def _row(r: Any) -> dict[str, Any]:
    return {
        "id": r.id,
        "email": r.primary_email,
        "display_name": r.display_name,
        "active": r.active,
        "is_admin": r.is_admin,
        "created_at": r.created_at,
    }


async def by_email(conn: AsyncConnection, tenant_id: UUID, email: str) -> list[Any]:
    """Every person whose email matches, ignoring case."""
    rows = await conn.execute(
        select(*FIELDS).where(
            _u.tenant_id == tenant_id, func.lower(_u.primary_email) == email.strip().lower()
        )
    )
    return list(rows.all())


async def list_people(
    engine: AsyncEngine,
    tenant_id: UUID,
    *,
    active: bool | None = None,
    query: str | None = None,
    limit: int = 100,
    cursor: tuple[datetime, UUID] | None = None,
) -> tuple[list[dict[str, Any]], tuple[datetime, UUID] | None]:
    stmt = select(*FIELDS).where(_u.tenant_id == tenant_id)
    if active is not None:
        stmt = stmt.where(_u.active.is_(active))
    if query:
        like = f"%{query.strip().lower()}%"
        stmt = stmt.where(
            or_(func.lower(_u.primary_email).like(like), func.lower(_u.display_name).like(like))
        )
    if cursor is not None:
        stmt = stmt.where(tuple_(_u.created_at, _u.id) < tuple_(*cursor))
    stmt = stmt.order_by(_u.created_at.desc(), _u.id.desc()).limit(limit + 1)
    async with tenant_context(engine, tenant_id) as conn:
        rows = [_row(r) for r in (await conn.execute(stmt)).all()]
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, ((rows[-1]["created_at"], rows[-1]["id"]) if more else None)


async def get_person(engine: AsyncEngine, tenant_id: UUID, user_id: UUID) -> dict[str, Any] | None:
    async with tenant_context(engine, tenant_id) as conn:
        row = (
            await conn.execute(select(*FIELDS).where(_u.tenant_id == tenant_id, _u.id == user_id))
        ).first()
    return _row(row) if row is not None else None


async def find_id(engine: AsyncEngine, tenant_id: UUID, email: str) -> UUID | None:
    async with tenant_context(engine, tenant_id) as conn:
        rows = await by_email(conn, tenant_id, email)
    exact = [r for r in rows if r.primary_email == email.strip()]
    found = exact or rows
    return found[0].id if len(found) == 1 else None


async def set_admin(
    engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    is_admin: bool,
    actor: audit.Actor,
) -> dict[str, Any] | None:
    """The person after the change, or None when there is no such person."""
    async with tenant_context(engine, tenant_id) as conn:
        person = await apply_admin(conn, tenant_id, user_id, is_admin, actor, source="admin")
    return person


async def apply_admin(
    conn: AsyncConnection,
    tenant_id: UUID,
    user_id: UUID,
    is_admin: bool,
    actor: audit.Actor,
    source: str,
) -> dict[str, Any] | None:
    this = (_u.tenant_id == tenant_id) & (_u.id == user_id)
    row = (await conn.execute(select(*FIELDS).where(this).with_for_update())).first()
    if row is None:
        return None
    if row.is_admin != is_admin:
        await conn.execute(
            update(EnterpriseUser).where(this).values(is_admin=is_admin, updated_at=func.now())
        )
        await audit.record(
            conn,
            tenant_id,
            actor,
            "user.admin_changed",
            params={"email": row.primary_email, "is_admin": is_admin, "source": source},
            record_ids=[user_id],
        )
    return {**_row(row), "is_admin": is_admin}


async def set_active(
    engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    active: bool,
    actor: audit.Actor,
) -> dict[str, Any] | None:
    """Deactivating also ends every session of the person at once."""
    this = (_u.tenant_id == tenant_id) & (_u.id == user_id)
    async with tenant_context(engine, tenant_id) as conn:
        row = (await conn.execute(select(*FIELDS).where(this).with_for_update())).first()
        if row is None:
            return None
        ended: list[UUID] = []
        if not active:
            ended = await sessions.revoke_for_user(conn, tenant_id, user_id)
        if row.active != active:
            await conn.execute(
                update(EnterpriseUser).where(this).values(active=active, updated_at=func.now())
            )
        if row.active != active or ended:
            await audit.record(
                conn,
                tenant_id,
                actor,
                "user.reactivated" if active else "user.deactivated",
                params={
                    "email": row.primary_email,
                    "active": active,
                    "sessions_revoked": len(ended) or None,
                },
                record_ids=[user_id],
            )
    if row.active != active:
        from checkcord.services import connection_audiences

        await connection_audiences.resolve_again(engine, tenant_id)
    return {**_row(row), "active": active, "sessions_revoked": len(ended)}
