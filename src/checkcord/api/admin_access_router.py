"""Admin endpoints for API keys, people and browser sessions."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.api.admin_router import _redis, run_purge
from checkcord.api.deps import (
    Principal,
    forget_key,
    forget_user,
    get_db_engine,
    require_admin_principal,
)
from checkcord.api.schemas import ERRORS, Page, decode_cursor, encode_cursor
from checkcord.core.config import get_settings
from checkcord.services import accounts, keys, people, purge, sessions

router = APIRouter(prefix="/api/v1/admin", responses=ERRORS)

_KEYS, _PEOPLE, _SESSIONS = ["admin: keys"], ["admin: people"], ["admin: sessions"]
_ACCOUNTS = ["admin: accounts"]


class KeyOut(BaseModel):
    id: UUID
    name: str
    is_admin: bool
    active: bool
    created_at: datetime
    user_id: UUID | None
    user_email: str | None


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    admin: bool = False
    user_email: str | None = Field(None, description="Bind the key to this person")
    create_user: bool = Field(False, description="Add the person when there is none")


class KeyCreated(BaseModel):
    id: UUID
    name: str
    is_admin: bool
    user_email: str | None
    key: str = Field(description="Shown once; only its hash is stored")


class Revoked(BaseModel):
    id: UUID
    revoked: bool


class Person(BaseModel):
    id: UUID
    email: str
    display_name: str | None
    active: bool
    is_admin: bool
    created_at: datetime


class PersonDetail(Person):
    keys: list[KeyOut]
    active_sessions: int


class PersonChanged(Person):
    sessions_revoked: int = 0


class AdminFlag(BaseModel):
    is_admin: bool


class SessionOut(BaseModel):
    id: UUID
    user_id: UUID
    user_email: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    active: bool
    user_agent: str | None
    source: str | None


def _key_out(k: keys.KeyInfo) -> KeyOut:
    return KeyOut(
        id=k.id,
        name=k.name,
        is_admin=k.is_admin,
        active=k.active,
        created_at=k.created_at,
        user_id=k.user_id,
        user_email=k.user_email,
    )


@router.get("/keys", tags=_KEYS, summary="List API keys")
async def list_keys(
    user_id: UUID | None = Query(None, description="Only keys bound to this person"),
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> Page[KeyOut]:
    """Every key of the tenant, oldest first. The key itself is never returned."""
    found = await keys.list_keys(db_engine, principal.tenant_id, user_id)
    return Page(items=[_key_out(k) for k in found])


@router.post("/keys", tags=_KEYS, summary="Create an API key", status_code=201)
async def create_key(
    body: KeyCreate,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> KeyCreated:
    """The response is the only place the key appears."""
    if body.create_user and not body.user_email:
        raise HTTPException(status_code=422, detail="create_user needs user_email")
    try:
        key_id, token = await keys.create_key(
            db_engine,
            principal.tenant_id,
            body.name,
            body.admin,
            body.user_email,
            body.create_user,
            actor=principal.as_actor(),
        )
    except keys.UserNotFoundError as err:
        raise HTTPException(status_code=404, detail=f"no active person {err}") from err
    return KeyCreated(
        id=key_id,
        name=body.name,
        is_admin=body.admin,
        user_email=body.user_email.strip() if body.user_email else None,
        key=token,
    )


@router.post("/keys/{key_id}/revoke", tags=_KEYS, summary="Revoke an API key")
async def revoke_key(
    key_id: UUID,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> Revoked:
    if not await keys.revoke_key(db_engine, principal.tenant_id, key_id, principal.as_actor()):
        raise HTTPException(status_code=404, detail="no such API key")
    forget_key(key_id)
    return Revoked(id=key_id, revoked=True)


@router.get("/users", tags=_PEOPLE, summary="List people")
async def list_people(
    active: bool | None = Query(None),
    q: str | None = Query(None, max_length=200, description="Part of the email or name"),
    limit: int = Query(100, ge=1, le=500),
    cursor: str | None = Query(None),
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> Page[Person]:
    rows, after = await people.list_people(
        db_engine,
        principal.tenant_id,
        active=active,
        query=q,
        limit=limit,
        cursor=decode_cursor(cursor),
    )
    return Page(items=[Person(**r) for r in rows], next_cursor=encode_cursor(after))


async def _person(db_engine: AsyncEngine, tenant_id: UUID, user_id: UUID) -> dict[str, Any]:
    found = await people.get_person(db_engine, tenant_id, user_id)
    if found is None:
        raise HTTPException(status_code=404, detail="no such person")
    return found


@router.get("/users/{user_id}", tags=_PEOPLE, summary="Show a person")
async def show_person(
    user_id: UUID,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> PersonDetail:
    found = await _person(db_engine, principal.tenant_id, user_id)
    bound = await keys.list_keys(db_engine, principal.tenant_id, user_id)
    live, _ = await sessions.list_sessions(
        db_engine, principal.tenant_id, get_settings(), user_id=user_id, limit=1000
    )
    return PersonDetail(**found, keys=[_key_out(k) for k in bound], active_sessions=len(live))


@router.put("/users/{user_id}/admin", tags=_PEOPLE, summary="Set or clear the admin flag")
async def set_admin(
    user_id: UUID,
    body: AdminFlag,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> Person:
    """SCIM_ADMIN_GROUPS or OIDC_ADMIN_GROUPS_CLAIM can set the flag again later."""
    found = await people.set_admin(
        db_engine, principal.tenant_id, user_id, body.is_admin, principal.as_actor()
    )
    if found is None:
        raise HTTPException(status_code=404, detail="no such person")
    return Person(**found)


@router.post("/users/{user_id}/deactivate", tags=_PEOPLE, summary="Deactivate a person")
async def deactivate(
    user_id: UUID,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> PersonChanged:
    """Their keys and sessions stop working at once."""
    found = await people.set_active(
        db_engine, principal.tenant_id, user_id, False, principal.as_actor()
    )
    if found is None:
        raise HTTPException(status_code=404, detail="no such person")
    forget_user(user_id)
    return PersonChanged(**found)


@router.post("/users/{user_id}/reactivate", tags=_PEOPLE, summary="Reactivate a person")
async def reactivate(
    user_id: UUID,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> PersonChanged:
    found = await people.set_active(
        db_engine, principal.tenant_id, user_id, True, principal.as_actor()
    )
    if found is None:
        raise HTTPException(status_code=404, detail="no such person")
    return PersonChanged(**found)


@router.delete("/users/{user_id}", tags=_PEOPLE, summary="Delete a person")
async def delete_person(
    user_id: UUID,
    request: Request,
    dry_run: bool = Query(False, description="Only count what would be deleted"),
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, Any]:
    """Deletes the person, their keys and sessions, and the events they wrote."""
    found = await purge.find_person(db_engine, principal.tenant_id, user_id=user_id)
    if found is None:
        raise HTTPException(status_code=404, detail="no such person")
    if dry_run:
        return await run_purge(request, principal, db_engine, found[1], dry_run=True)
    ctx = getattr(request.app.state, "ctx", None)
    deleted = await purge.delete_person(
        db_engine,
        principal.tenant_id,
        actor=principal.as_actor(),
        user_id=user_id,
        redis=ctx.redis_client if ctx else None,
    )
    if deleted is None:
        raise HTTPException(status_code=404, detail="no such person")
    forget_user(user_id)
    return {
        "dry_run": False,
        "user_id": str(user_id),
        "keys": deleted.keys,
        **deleted.purged.counts(),
    }


@router.get("/sessions", tags=_SESSIONS, summary="List sessions")
async def list_sessions(
    user_id: UUID | None = Query(None),
    include_ended: bool = Query(False, description="Also revoked and expired sessions"),
    limit: int = Query(100, ge=1, le=500),
    cursor: str | None = Query(None),
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> Page[SessionOut]:
    """Newest first. The session cookie and its hash are never returned."""
    rows, after = await sessions.list_sessions(
        db_engine,
        principal.tenant_id,
        get_settings(),
        user_id=user_id,
        include_ended=include_ended,
        limit=limit,
        cursor=decode_cursor(cursor),
    )
    return Page(items=[SessionOut(**r) for r in rows], next_cursor=encode_cursor(after))


@router.post("/sessions/{session_id}/revoke", tags=_SESSIONS, summary="Revoke a session")
async def revoke_session(
    session_id: UUID,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> Revoked:
    if not await sessions.revoke(db_engine, principal.tenant_id, session_id, principal.as_actor()):
        raise HTTPException(status_code=404, detail="no live session with that id")
    return Revoked(id=session_id, revoked=True)


class AccountOut(BaseModel):
    platform: str
    account: str = Field(description="The platform's user id, or the GitHub login")
    display_name: str | None
    source_email: str | None = Field(description="The email the platform gave")
    user_id: UUID | None
    person_email: str | None = Field(description="The person it is matched to")
    link_source: str | None = Field(description="email, profile or manual")
    linked_at: datetime | None
    linked_by: str | None
    unmatched_in: int = Field(description="Restricted audiences that list it as not matched")


class AccountLink(BaseModel):
    platform: str = Field(description="slack, teams or github")
    account: str = Field(min_length=1, max_length=200)
    email: str | None = None
    user_id: UUID | None = None


class AccountUnlink(BaseModel):
    platform: str
    account: str = Field(min_length=1, max_length=200)


def _account_cursor(cursor: str | None) -> tuple[str, str] | None:
    import base64

    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        platform, account = raw.split("|", 1)
        return platform, account
    except (ValueError, UnicodeDecodeError) as err:
        raise HTTPException(status_code=400, detail="invalid cursor") from err


def _account_next(position: tuple[str, str] | None) -> str | None:
    import base64

    if position is None:
        return None
    return base64.urlsafe_b64encode("|".join(position).encode()).decode().rstrip("=")


@router.get("/accounts", tags=_ACCOUNTS, summary="List accounts and their people")
async def list_accounts(
    platform: str | None = Query(None, description="slack, teams or github"),
    matched: bool | None = Query(None, description="false: only accounts with no person"),
    limit: int = Query(100, ge=1, le=500),
    cursor: str | None = Query(None),
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> Page[AccountOut]:
    try:
        rows, after = await accounts.list_accounts(
            db_engine,
            principal.tenant_id,
            platform=platform,
            matched=matched,
            limit=limit,
            cursor=_account_cursor(cursor),
        )
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return Page(items=[AccountOut(**r) for r in rows], next_cursor=_account_next(after))


@router.post("/accounts/link", tags=_ACCOUNTS, summary="Match an account to a person")
async def link_account(
    body: AccountLink,
    request: Request,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, Any]:
    """A sync never changes a link made here. An account not seen yet is created."""
    if (body.email is None) == (body.user_id is None):
        raise HTTPException(status_code=422, detail="give email or user_id")
    try:
        done = await accounts.link(
            db_engine,
            principal.tenant_id,
            body.platform,
            body.account,
            actor=principal.as_actor(),
            email=body.email,
            user_id=body.user_id,
            redis=_redis(request),
        )
    except accounts.UnknownPerson as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return {
        "platform": done.platform,
        "account": done.account,
        "user_id": done.user_id,
        "person_email": done.person_email,
        "created": done.created,
    }


@router.post("/accounts/unlink", tags=_ACCOUNTS, summary="Take an account's person away")
async def unlink_account(
    body: AccountUnlink,
    request: Request,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, Any]:
    """A sync does not match the account again until it is linked by hand."""
    try:
        done = await accounts.unlink(
            db_engine,
            principal.tenant_id,
            body.platform,
            body.account,
            actor=principal.as_actor(),
            redis=_redis(request),
        )
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    if done is None:
        raise HTTPException(status_code=404, detail="no such account")
    return {"platform": done.platform, "account": done.account, "unlinked": True}
