"""Request authentication: a Bearer API key or a browser session cookie."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast
from uuid import UUID

import structlog
from fastapi import Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.api import rate_limit
from checkcord.core import metrics
from checkcord.core.config import get_settings
from checkcord.core.crypto import hash_api_key
from checkcord.llm import usage
from checkcord.storage import audit
from checkcord.storage.models import APIKey, EnterpriseUser
from checkcord.storage.postgres import app_bypass_rls_context, tenant_context

logger = structlog.get_logger(__name__)


class SimpleTTLCache[T]:
    """TTL cache for resolved keys."""

    def __init__(self, maxsize: int = 1000, ttl: float = 300.0) -> None:
        self._maxsize = maxsize
        self._ttl = ttl
        self._cache: dict[str, tuple[T, float]] = {}

    def __setitem__(self, key: str, value: T) -> None:
        if len(self._cache) >= self._maxsize:
            # Evict oldest entry
            oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k][1])
            del self._cache[oldest_key]
        self._cache[key] = (value, time.time())

    def __getitem__(self, key: str) -> T:
        if key not in self._cache:
            raise KeyError(key)
        val, timestamp = self._cache[key]
        if time.time() - timestamp > self._ttl:
            del self._cache[key]
            raise KeyError(key)
        return val

    def __contains__(self, key: str) -> bool:
        if key not in self._cache:
            return False
        _, timestamp = self._cache[key]
        if time.time() - timestamp > self._ttl:
            del self._cache[key]
            return False
        return True

    def clear(self) -> None:
        self._cache.clear()

    def discard_where(self, predicate: Callable[[T], bool]) -> None:
        for key in [k for k, (v, _) in self._cache.items() if predicate(v)]:
            del self._cache[key]


TOKEN_CACHE_TTL_SECONDS = 300


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class Principal:
    """The caller behind a Bearer key or a session cookie."""

    tenant_id: UUID
    key_name: str
    is_admin: bool = False
    key_id: UUID | None = None
    user_id: UUID | None = None
    user_email: str | None = None
    session_id: UUID | None = None

    @property
    def identity(self) -> UUID:
        """The ``user_id`` for ``tenant_context``; unbound keys get the anonymous identity."""
        from checkcord.core.identity import ANONYMOUS_USER_ID

        return self.user_id or ANONYMOUS_USER_ID

    @property
    def actor(self) -> str:
        return self.user_email or f"key:{self.key_name}"

    def as_actor(self, channel: str = audit.API) -> audit.Actor:
        return audit.Actor(
            channel=channel,
            key_id=self.key_id,
            key_name=self.key_name,
            user_id=self.user_id,
            user_email=self.user_email,
        )


_TOKEN_TENANT_CACHE: SimpleTTLCache[Principal] = SimpleTTLCache(
    maxsize=1000, ttl=float(TOKEN_CACHE_TTL_SECONDS)
)


def forget_user(user_id: UUID) -> None:
    """Stop accepting a deleted or deactivated person's cached tokens in this process."""
    _TOKEN_TENANT_CACHE.discard_where(lambda p: p.user_id == user_id)


def forget_key(key_id: UUID) -> None:
    _TOKEN_TENANT_CACHE.discard_where(lambda p: p.key_id == key_id)


async def get_db_engine(request: Request) -> AsyncEngine:
    """The app's AsyncEngine, created on first use."""
    ctx = getattr(request.app.state, "ctx", None)
    if ctx and hasattr(ctx, "db_engine") and ctx.db_engine:
        return cast(AsyncEngine, ctx.db_engine)
    engine = getattr(request.app.state, "db_engine", None)
    if not engine:
        settings = get_settings()
        engine = create_async_engine(settings.database_url)
        request.app.state.db_engine = engine
    return engine


async def _principal_from_db(hashed: str, db_engine: AsyncEngine) -> Principal | None:
    async with app_bypass_rls_context(db_engine) as conn:
        row = (
            await conn.execute(
                select(
                    APIKey.id, APIKey.tenant_id, APIKey.name, APIKey.is_admin, APIKey.user_id
                ).where(APIKey.hashed_token == hashed, APIKey.active.is_(True))
            )
        ).first()
    if row is None:
        return None

    email: str | None = None
    if row.user_id is not None:
        async with tenant_context(db_engine, row.tenant_id) as conn:
            email = await conn.scalar(
                select(EnterpriseUser.primary_email).where(
                    EnterpriseUser.id == row.user_id,
                    EnterpriseUser.tenant_id == row.tenant_id,
                    EnterpriseUser.active.is_(True),
                )
            )
        if email is None:
            logger.warning("api_key_user_inactive", key_id=str(row.id))
            return None

    return Principal(
        tenant_id=row.tenant_id,
        key_name=row.name,
        is_admin=row.is_admin,
        key_id=row.id,
        user_id=row.user_id,
        user_email=email,
    )


async def resolve_principal(token: str, db_engine: AsyncEngine | None = None) -> Principal | None:
    """Resolve a Bearer token to the Principal of its active api_keys row."""
    if not token:
        return None

    hashed = hash_api_key(token)
    if hashed in _TOKEN_TENANT_CACHE:
        return _TOKEN_TENANT_CACHE[hashed]

    if db_engine:
        try:
            principal = await _principal_from_db(hashed, db_engine)
            if principal is not None:
                _TOKEN_TENANT_CACHE[hashed] = principal
                return principal
        except Exception as err:
            logger.warning("api_keys_db_lookup_failed", error=str(err))

    return None


def channel_for(request: Request) -> str:
    path = request.url.path
    if path.startswith("/mcp"):
        return audit.MCP
    return audit.SCIM if path.startswith("/scim") else audit.API


AUTH_FAILURES_PER_MINUTE = 100
_auth_failures: dict[int, set[tuple[str, str]]] = {}


def _first_failure_this_minute(key_prefix: str, source: str) -> bool:
    """One entry per key prefix and source per minute, and at most 100 a minute in all."""
    minute = int(time.time() // 60)
    for old in [m for m in _auth_failures if m != minute]:
        del _auth_failures[old]
    seen = _auth_failures.setdefault(minute, set())
    if (key_prefix, source) in seen or len(seen) >= AUTH_FAILURES_PER_MINUTE:
        return False
    seen.add((key_prefix, source))
    return True


async def record_auth_failure(request: Request, token: str, db_engine: AsyncEngine | None) -> None:
    """Record a rejected token in the tenant it belongs to, or DEFAULT_TENANT_ID."""
    metrics.AUTH_FAILURES.labels(channel_for(request)).inc()
    source = request.client.host if request.client else "unknown"
    key_prefix = token[:8]
    if db_engine is None or not _first_failure_this_minute(key_prefix, source):
        return
    try:
        async with app_bypass_rls_context(db_engine) as conn:
            key = (
                await conn.execute(
                    select(APIKey.id, APIKey.tenant_id, APIKey.name, APIKey.active).where(
                        APIKey.hashed_token == hash_api_key(token)
                    )
                )
            ).first()
        if key is None:
            reason, actor = "unknown key", audit.Actor(channel=channel_for(request))
        else:
            reason = "revoked key" if not key.active else "inactive user"
            actor = audit.Actor(channel=channel_for(request), key_id=key.id, key_name=key.name)
        tenant_id = key.tenant_id if key is not None else get_settings().default_tenant_id
        params = {"key_prefix": key_prefix, "source": source, "path": request.url.path}
        if tenant_id is None:
            audit.emit_lines([{"action": "auth.failed", "reason": reason, **params}])
            return
        async with tenant_context(db_engine, tenant_id) as conn:
            await audit.record(
                conn, tenant_id, actor, "auth.failed", params={"reason": reason, **params}
            )
    except Exception as err:
        logger.warning("auth_failure_not_recorded", error=str(err))


def bearer_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization[7:].strip() or None


def _authenticated(request: Request, principal: Principal) -> Principal:
    request.state.tenant_id = principal.tenant_id
    request.state.principal = principal
    usage.set_tenant(principal.tenant_id)
    return principal


async def _key_principal(request: Request, token: str | None) -> Principal:
    if token is None:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Missing or invalid token format",
        )

    db_engine = None
    try:
        db_engine = await get_db_engine(request)
    except Exception:
        pass

    principal = await resolve_principal(token, db_engine)
    if principal is None:
        await record_auth_failure(request, token, db_engine)
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Invalid token",
        )
    await rate_limit.principal_request(request, principal)
    return _authenticated(request, principal)


async def _session_principal(request: Request, token: str) -> Principal:
    from checkcord.services import sessions

    settings = get_settings()
    found = None
    try:
        found = await sessions.resolve(await get_db_engine(request), token, settings)
    except Exception as err:
        logger.warning("session_lookup_failed", error=str(err))
    if found is None:
        metrics.AUTH_FAILURES.labels(channel_for(request)).inc()
        raise HTTPException(
            status_code=401, detail="Unauthorized: Session expired or revoked; sign in again"
        )
    if request.method not in SAFE_METHODS and not sessions.csrf_matches(
        token, request.headers.get(sessions.CSRF_HEADER)
    ):
        raise HTTPException(status_code=403, detail="Forbidden: missing or invalid CSRF token")
    request.state.session = found
    principal = Principal(
        tenant_id=found.tenant_id,
        key_name=sessions.ACTOR_KEY_NAME,
        is_admin=found.is_admin,
        user_id=found.user_id,
        user_email=found.user_email,
        session_id=found.id,
    )
    await rate_limit.principal_request(request, principal)
    return _authenticated(request, principal)


def session_cookie(request: Request) -> str | None:
    from checkcord.services.sessions import COOKIE

    if not get_settings().sign_in_enabled:
        return None
    return request.cookies.get(COOKIE) or None


async def get_principal(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Principal:
    """A Bearer key wins over the session cookie; 401 when neither is valid."""
    token = bearer_token(authorization)
    cookie = session_cookie(request) if token is None else None
    if cookie is not None:
        return await _session_principal(request, cookie)
    return await _key_principal(request, token)


async def require_admin_principal(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Principal:
    """Like ``get_principal`` but 403 unless the key or the signed-in person is an admin."""
    principal = await get_principal(request, authorization)
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden: admin rights required")
    return principal


async def require_admin_key(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Principal:
    """Admin Bearer key only, for machine clients such as SCIM."""
    principal = await _key_principal(request, bearer_token(authorization))
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden: admin rights required")
    return principal


async def verify_hybrid_bearer_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> UUID:
    """Bearer key only: the ingestion endpoints are for machines, not browsers."""
    principal = await _key_principal(request, bearer_token(authorization))
    return principal.tenant_id
