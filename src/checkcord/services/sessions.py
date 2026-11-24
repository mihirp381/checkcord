"""Browser sessions: created at sign-in, checked on every request, never cached."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from checkcord.core.config import Settings
from checkcord.core.crypto import hash_api_key
from checkcord.storage import audit
from checkcord.storage.models import EnterpriseUser, UserSession
from checkcord.storage.postgres import app_bypass_rls_context, tenant_context

COOKIE = "checkcord_session"
ACTOR_KEY_NAME = "session"
CSRF_HEADER = "X-CSRF-Token"
TOUCH_AFTER = timedelta(minutes=1)

_s = UserSession.__table__.c
_u = EnterpriseUser.__table__.c


def new_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_token(session_token: str) -> str:
    return hmac.new(session_token.encode(), b"checkcord-csrf", hashlib.sha256).hexdigest()


def csrf_matches(session_token: str, presented: str | None) -> bool:
    return bool(presented) and hmac.compare_digest(csrf_token(session_token), presented or "")


@dataclass(frozen=True)
class Session:
    id: UUID
    tenant_id: UUID
    user_id: UUID
    user_email: str
    is_admin: bool
    expires_at: datetime
    idle_expires_at: datetime


def _live(now: datetime, settings: Settings) -> Any:
    idle = timedelta(minutes=settings.session_idle_minutes)
    return _s.revoked_at.is_(None) & (_s.expires_at > now) & (_s.last_seen_at > now - idle)


async def create(
    conn: AsyncConnection,
    tenant_id: UUID,
    user_id: UUID,
    settings: Settings,
    user_agent: str | None = None,
    source: str | None = None,
) -> tuple[UUID, str]:
    token, session_id = new_token(), uuid4()
    now = datetime.now(UTC)
    await conn.execute(
        UserSession.__table__.insert().values(
            id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            hashed_token=hash_api_key(token),
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(hours=settings.session_max_hours),
            user_agent=(user_agent or "")[:300] or None,
            source=source,
        )
    )
    return session_id, token


async def resolve(engine: AsyncEngine, token: str, settings: Settings) -> Session | None:
    """The live session of an active person, or None. Touches last_seen_at at most once a minute."""
    now = datetime.now(UTC)
    async with app_bypass_rls_context(engine) as conn:
        found = (
            await conn.execute(
                select(_s.id, _s.tenant_id, _s.user_id, _s.expires_at, _s.last_seen_at).where(
                    _s.hashed_token == hash_api_key(token), _live(now, settings)
                )
            )
        ).first()
    if found is None:
        return None
    async with tenant_context(engine, found.tenant_id) as conn:
        person = (
            await conn.execute(
                select(_u.primary_email, _u.is_admin).where(
                    _u.tenant_id == found.tenant_id, _u.id == found.user_id, _u.active.is_(True)
                )
            )
        ).first()
        if person is None:
            return None
        last_seen = found.last_seen_at
        if now - last_seen >= TOUCH_AFTER:
            await conn.execute(
                update(UserSession).where(_s.id == found.id).values(last_seen_at=now)
            )
            last_seen = now
    idle = timedelta(minutes=settings.session_idle_minutes)
    return Session(
        id=found.id,
        tenant_id=found.tenant_id,
        user_id=found.user_id,
        user_email=person.primary_email,
        is_admin=person.is_admin,
        expires_at=found.expires_at,
        idle_expires_at=min(found.expires_at, last_seen + idle),
    )


async def revoke_for_user(conn: AsyncConnection, tenant_id: UUID, user_id: UUID) -> list[UUID]:
    result = await conn.execute(
        update(UserSession)
        .where(_s.tenant_id == tenant_id, _s.user_id == user_id, _s.revoked_at.is_(None))
        .values(revoked_at=func.now())
        .returning(_s.id)
    )
    return list(result.scalars())


async def revoke(
    engine: AsyncEngine,
    tenant_id: UUID,
    session_id: UUID,
    actor: audit.Actor,
    action: str = "session.revoked",
) -> bool:
    async with tenant_context(engine, tenant_id) as conn:
        user_id = await conn.scalar(
            update(UserSession)
            .where(_s.tenant_id == tenant_id, _s.id == session_id, _s.revoked_at.is_(None))
            .values(revoked_at=func.now())
            .returning(_s.user_id)
        )
        if user_id is None:
            return False
        await audit.record(
            conn,
            tenant_id,
            actor,
            action,
            params={"user_id": user_id},
            record_ids=[session_id],
        )
    return True


async def list_sessions(
    engine: AsyncEngine,
    tenant_id: UUID,
    settings: Settings,
    *,
    user_id: UUID | None = None,
    include_ended: bool = False,
    limit: int = 100,
    cursor: tuple[datetime, UUID] | None = None,
) -> tuple[list[dict[str, Any]], tuple[datetime, UUID] | None]:
    """Newest first. Never returns the token or its hash."""
    now = datetime.now(UTC)
    stmt = (
        select(
            _s.id,
            _s.user_id,
            _u.primary_email.label("user_email"),
            _s.created_at,
            _s.last_seen_at,
            _s.expires_at,
            _s.revoked_at,
            _s.user_agent,
            _s.source,
            _live(now, settings).label("active"),
        )
        .join(EnterpriseUser.__table__, (_u.id == _s.user_id) & (_u.tenant_id == _s.tenant_id))
        .where(_s.tenant_id == tenant_id)
    )
    if user_id is not None:
        stmt = stmt.where(_s.user_id == user_id)
    if not include_ended:
        stmt = stmt.where(_live(now, settings) & _u.active.is_(True))
    if cursor is not None:
        stmt = stmt.where(tuple_(_s.created_at, _s.id) < tuple_(*cursor))
    stmt = stmt.order_by(_s.created_at.desc(), _s.id.desc()).limit(limit + 1)
    async with tenant_context(engine, tenant_id) as conn:
        rows = [dict(r) for r in (await conn.execute(stmt)).mappings()]
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, ((rows[-1]["created_at"], rows[-1]["id"]) if more else None)
