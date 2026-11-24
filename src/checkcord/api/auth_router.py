"""Browser sign-in with OpenID Connect, and who the caller is."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.api.deps import (
    Principal,
    _first_failure_this_minute,
    get_db_engine,
    get_principal,
)
from checkcord.api.schemas import ERRORS
from checkcord.core import metrics
from checkcord.core.config import Settings, get_settings
from checkcord.core.oidc import OIDCClient, SignInError, pkce_pair
from checkcord.services import sessions, sign_in
from checkcord.storage import audit
from checkcord.storage.postgres import tenant_context

logger = structlog.get_logger(__name__).bind(component="auth_router")

router = APIRouter(prefix="/api/v1/auth", tags=["sign-in"], responses=ERRORS)

LOGIN_COOKIE = "checkcord_login"
LOGIN_COOKIE_PATH = "/api/v1/auth"
STATE_TTL_SECONDS = 600
_STATE_PREFIX = "oidc:login:"
_IDP_ERROR = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


class SignInConfig(BaseModel):
    sign_in_enabled: bool
    login_url: str | None


class Me(BaseModel):
    tenant_id: UUID
    user_id: UUID | None
    email: str | None
    is_admin: bool
    auth: Literal["session", "key"]
    key_id: UUID | None = None
    key_name: str | None = None
    session_id: UUID | None = None
    session_expires_at: datetime | None = None
    session_idle_expires_at: datetime | None = None
    csrf_token: str | None = None
    csrf_header: str | None = None


def _secure(settings: Settings) -> bool:
    return settings.environment != "development"


def _enabled(settings: Settings) -> tuple[UUID, str]:
    if not settings.sign_in_enabled:
        raise HTTPException(
            status_code=404,
            detail="Sign-in is not configured on this server; use an API key.",
        )
    redirect_uri = settings.effective_oidc_redirect_uri
    if settings.default_tenant_id is None or redirect_uri is None:
        raise HTTPException(
            status_code=503,
            detail="Sign-in needs DEFAULT_TENANT_ID and OIDC_REDIRECT_URI or PUBLIC_BASE_URL.",
        )
    return settings.default_tenant_id, redirect_uri


def _oidc(request: Request, settings: Settings) -> OIDCClient:
    client = getattr(request.app.state, "oidc", None)
    if client is None:
        client = OIDCClient(settings)
        request.app.state.oidc = client
    return client


def _redis(request: Request, settings: Settings) -> Redis:
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is not None and getattr(ctx, "redis_client", None) is not None:
        return ctx.redis_client  # type: ignore[no-any-return]
    client = getattr(request.app.state, "sign_in_redis", None)
    if client is None:
        client = Redis.from_url(settings.redis_url)
        request.app.state.sign_in_redis = client
    return client


def _state_key(state: str) -> str:
    return _STATE_PREFIX + hashlib.sha256(state.encode()).hexdigest()


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _return_to(value: str | None, settings: Settings) -> str:
    if not value:
        return settings.web_base_url
    allowed = {_origin(o) for o in settings.cors_allowed_origins} | {_origin(settings.web_base_url)}
    if urlsplit(value).scheme not in ("http", "https") or _origin(value) not in allowed:
        raise HTTPException(status_code=400, detail="return_to must be on an allowed web origin")
    return value


def _failed(settings: Settings, code: str) -> RedirectResponse:
    response = RedirectResponse(
        f"{settings.web_base_url.rstrip('/')}/sign-in?{urlencode({'error': code})}",
        status_code=303,
    )
    response.delete_cookie(LOGIN_COOKIE, path=LOGIN_COOKIE_PATH)
    return response


async def _refused(
    request: Request,
    engine: AsyncEngine,
    tenant_id: UUID,
    reason: str,
    detail: str,
    email: str | None = None,
) -> None:
    metrics.SIGN_IN_FAILURES.labels(reason).inc()
    source = request.client.host if request.client else "unknown"
    if not _first_failure_this_minute(f"oidc:{reason}", source):
        return
    try:
        async with tenant_context(engine, tenant_id) as conn:
            await audit.record(
                conn,
                tenant_id,
                sign_in.session_actor(None, email),
                "auth.sign_in_refused",
                params={"reason": reason, "detail": detail, "source": source},
            )
    except Exception as err:
        logger.warning("sign_in_refusal_not_recorded", error=str(err))


@router.get("/config", summary="Whether browser sign-in is available")
async def sign_in_config() -> SignInConfig:
    enabled = get_settings().sign_in_enabled
    return SignInConfig(
        sign_in_enabled=enabled, login_url=f"{router.prefix}/login" if enabled else None
    )


@router.get(
    "/login",
    summary="Start sign-in",
    description="Redirects to the identity provider. `return_to` must be on an allowed web "
    "origin; the browser comes back there after sign-in.",
    status_code=302,
    response_class=RedirectResponse,
)
async def login(
    request: Request, return_to: str | None = Query(None, description="Where to land afterwards")
) -> RedirectResponse:
    settings = get_settings()
    _, redirect_uri = _enabled(settings)
    target = _return_to(return_to, settings)
    client = _oidc(request, settings)
    try:
        meta = await client.metadata()
    except SignInError as err:
        raise HTTPException(status_code=502, detail=f"Identity provider: {err}") from err
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    verifier, challenge = pkce_pair()
    await _redis(request, settings).set(
        _state_key(state),
        json.dumps({"nonce": nonce, "verifier": verifier, "return_to": target}),
        ex=STATE_TTL_SECONDS,
    )
    response = RedirectResponse(
        client.authorization_url(meta, redirect_uri, state, nonce, challenge), status_code=302
    )
    response.set_cookie(
        LOGIN_COOKIE,
        state,
        max_age=STATE_TTL_SECONDS,
        path=LOGIN_COOKIE_PATH,
        httponly=True,
        secure=_secure(settings),
        samesite="lax",
    )
    return response


@router.get(
    "/callback",
    summary="Finish sign-in",
    description="The identity provider redirects here. On success the session cookie is set "
    "and the browser goes back to `return_to`. On failure the browser goes to the console "
    "sign-in page with `?error=` set to `bad_state`, `provider_error`, `invalid_token`, "
    "`unverified_email`, `ambiguous_person`, `deactivated_person` or `unknown_person`.",
    status_code=303,
    response_class=RedirectResponse,
)
async def callback(
    request: Request,
    state: str | None = Query(None),
    code: str | None = Query(None),
    error: str | None = Query(None),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> RedirectResponse:
    settings = get_settings()
    tenant_id, redirect_uri = _enabled(settings)
    stored = await _redis(request, settings).getdel(_state_key(state)) if state else None
    cookie = request.cookies.get(LOGIN_COOKIE)
    if stored is None or not state or not cookie or not hmac.compare_digest(cookie, state):
        await _refused(request, db_engine, tenant_id, "bad_state", "unknown or used state")
        return _failed(settings, "bad_state")
    if error is not None or not code:
        shown = error if error and _IDP_ERROR.match(error) else "error"
        await _refused(request, db_engine, tenant_id, "provider_error", shown)
        return _failed(settings, "provider_error")
    login_state: dict[str, Any] = json.loads(stored)
    client = _oidc(request, settings)
    try:
        id_token = await client.exchange(code, login_state["verifier"], redirect_uri)
        claims = await client.validate(id_token, login_state["nonce"])
    except SignInError as err:
        await _refused(request, db_engine, tenant_id, "invalid_token", str(err))
        return _failed(settings, "invalid_token")
    try:
        signed = await sign_in.complete(
            db_engine,
            tenant_id,
            claims,
            settings,
            user_agent=request.headers.get("user-agent"),
            source=request.client.host if request.client else None,
        )
    except sign_in.Refused as refused:
        await _refused(request, db_engine, tenant_id, refused.reason, refused.reason, refused.email)
        return _failed(settings, refused.reason)
    response = RedirectResponse(login_state["return_to"], status_code=303)
    response.set_cookie(
        sessions.COOKIE,
        signed.token,
        max_age=settings.session_max_hours * 3600,
        path="/",
        httponly=True,
        secure=_secure(settings),
        samesite="lax",
    )
    response.delete_cookie(LOGIN_COOKIE, path=LOGIN_COOKIE_PATH)
    return response


@router.post(
    "/logout",
    summary="End the current session",
    description="Needs the CSRF header when called with the session cookie. With an API key "
    "it does nothing.",
    status_code=204,
)
async def logout(
    request: Request,
    principal: Principal = Depends(get_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> Response:
    settings = get_settings()
    if principal.session_id is not None:
        await sessions.revoke(
            db_engine,
            principal.tenant_id,
            principal.session_id,
            principal.as_actor(),
            action="auth.signed_out",
        )
    response = Response(status_code=204)
    response.delete_cookie(
        sessions.COOKIE, path="/", httponly=True, secure=_secure(settings), samesite="lax"
    )
    return response


@router.get(
    "/me",
    summary="Who the caller is",
    description="With the session cookie, also returns the CSRF token that state-changing "
    "requests must send in the `X-CSRF-Token` header.",
)
async def me(request: Request, principal: Principal = Depends(get_principal)) -> Me:
    found: sessions.Session | None = getattr(request.state, "session", None)
    if principal.session_id is None or found is None:
        return Me(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            email=principal.user_email,
            is_admin=principal.is_admin,
            auth="key",
            key_id=principal.key_id,
            key_name=principal.key_name,
        )
    return Me(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        email=principal.user_email,
        is_admin=principal.is_admin,
        auth="session",
        session_id=found.id,
        session_expires_at=found.expires_at,
        session_idle_expires_at=found.idle_expires_at,
        csrf_token=sessions.csrf_token(request.cookies[sessions.COOKIE]),
        csrf_header=sessions.CSRF_HEADER,
    )
