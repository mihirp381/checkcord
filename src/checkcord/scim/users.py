"""SCIM Users: people, matched to every other system by their primary email."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from checkcord.api.deps import forget_user, get_db_engine
from checkcord.scim.protocol import (
    ENTERPRISE,
    READ_ONLY,
    USER,
    SCIMException,
    SCIMResponse,
    apply,
    as_bool,
    base_url,
    canon,
    list_response,
    not_found,
    operations,
    page,
    params,
    parse_filter,
    read_body,
)
from checkcord.services import connection_audiences, purge, sessions
from checkcord.storage import audit
from checkcord.storage.models import EnterpriseUser
from checkcord.storage.postgres import tenant_context

router = APIRouter()

_u = EnterpriseUser.__table__.c
_ATTRS = (
    "userName externalId displayName name emails active title nickName userType locale "
    "timezone preferredLanguage phoneNumbers addresses roles"
)
NAMES = {n.lower(): n for n in _ATTRS.split()}
_SUBS = {
    "name": {
        n.lower(): n
        for n in "formatted familyName givenName middleName honorificPrefix honorificSuffix".split()
    },
    "emails": {n.lower(): n for n in "value type primary display".split()},
}
FILTERS = {"username": "userName", "externalid": "externalId"}


def _name(attr: str) -> str:
    return NAMES.get(attr.lower(), attr)


def _tidy(resource: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in canon(resource, NAMES).items() if k.lower() not in READ_ONLY}
    if isinstance(out.get("name"), dict):
        out["name"] = canon(out["name"], _SUBS["name"])
    if isinstance(out.get("emails"), list):
        out["emails"] = [canon(e, _SUBS["emails"]) for e in out["emails"] if isinstance(e, dict)]
        for e in out["emails"]:
            if "primary" in e:
                e["primary"] = as_bool(e["primary"], "primary")
    return out


def _derive(resource: dict[str, Any]) -> tuple[str, str, bool]:
    user_name = resource.get("userName")
    if not isinstance(user_name, str) or not user_name.strip():
        raise SCIMException(400, "userName is required", "invalidValue")
    emails = [e for e in resource.get("emails") or [] if isinstance(e, dict) and e.get("value")]
    chosen = (
        next((e for e in emails if e.get("primary") is True), None)
        or next((e for e in emails if str(e.get("type", "")).lower() == "work"), None)
        or (emails[0] if emails else None)
    )
    email = str(chosen["value"]).strip() if chosen else user_name.strip()
    if "@" not in email:
        raise SCIMException(400, "A person needs an email address", "invalidValue")
    name = resource.get("name") if isinstance(resource.get("name"), dict) else {}
    assert isinstance(name, dict)
    display = (
        resource.get("displayName")
        or name.get("formatted")
        or " ".join(p for p in (name.get("givenName"), name.get("familyName")) if p)
        or user_name
    )
    return email, str(display), as_bool(resource.get("active", True))


def resource(row: Any, base: str) -> dict[str, Any]:
    stored = dict(row.scim_metadata or {})
    out: dict[str, Any] = {
        "schemas": [USER] + ([ENTERPRISE] if ENTERPRISE in stored else []),
        "id": str(row.id),
        **stored,
    }
    out.setdefault("userName", row.primary_email)
    out.setdefault("displayName", row.display_name)
    out.setdefault("name", {"formatted": row.display_name})
    out.setdefault("emails", [{"value": row.primary_email, "primary": True}])
    out["active"] = row.active
    out["meta"] = {
        "resourceType": "User",
        "created": row.created_at.isoformat(),
        "lastModified": row.updated_at.isoformat(),
        "location": f"{base}/Users/{row.id}",
    }
    return out


def _id(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError as err:
        raise not_found("User") from err


def _user_name() -> Any:
    return func.lower(func.coalesce(_u.scim_metadata["userName"].astext, _u.primary_email))


async def _taken(
    conn: AsyncConnection, tenant_id: UUID, email: str, user_name: str, but: UUID | None = None
) -> None:
    clash = (func.lower(_u.primary_email) == email.lower()) | (_user_name() == user_name.lower())
    where = (_u.tenant_id == tenant_id) & clash
    if but is not None:
        where &= _u.id != but
    if (await conn.execute(select(_u.id).where(where).limit(1))).first():
        raise SCIMException(409, "A person with this userName or email exists", "uniqueness")


async def _load(conn: AsyncConnection, tenant_id: UUID, user_id: UUID, lock: bool = False) -> Any:
    query = select(EnterpriseUser.__table__).where((_u.tenant_id == tenant_id) & (_u.id == user_id))
    row = (await conn.execute(query.with_for_update() if lock else query)).first()
    if row is None:
        raise not_found("User")
    return row


async def _record(
    conn: AsyncConnection,
    request: Request,
    user_id: UUID,
    email: str,
    was_active: bool | None,
    active: bool,
) -> None:
    tenant_id = request.state.tenant_id
    ended: list[UUID] = []
    if was_active is None:
        action = "user.created"
    elif was_active and not active:
        action = "user.deactivated"
        ended = await sessions.revoke_for_user(conn, tenant_id, user_id)
        forget_user(user_id)
    else:
        action = "user.updated"
    await audit.record(
        conn,
        tenant_id,
        request.state.principal.as_actor(audit.SCIM),
        action,
        params={"email": email, "active": active, "sessions_revoked": len(ended) or None},
        record_ids=[user_id],
    )


async def _save(
    conn: AsyncConnection, request: Request, user_id: UUID, data: dict[str, Any]
) -> Any:
    tenant_id = request.state.tenant_id
    email, display, active = _derive(data)
    before = await _load(conn, tenant_id, user_id, lock=True)
    await _taken(conn, tenant_id, email, data["userName"], but=user_id)
    await conn.execute(
        update(EnterpriseUser)
        .where((_u.tenant_id == tenant_id) & (_u.id == user_id))
        .values(
            primary_email=email,
            display_name=display,
            active=active,
            scim_metadata=data,
            updated_at=func.now(),
        )
    )
    await _record(conn, request, user_id, email, before.active, active)
    return await _load(conn, tenant_id, user_id)


@router.post("/Users", status_code=201, summary="Create a person")
async def create_user(
    request: Request, db_engine: AsyncEngine = Depends(get_db_engine)
) -> SCIMResponse:
    tenant_id = request.state.tenant_id
    data = _tidy(await read_body(request))
    email, display, active = _derive(data)
    user_id = uuid4()
    async with tenant_context(db_engine, tenant_id) as conn:
        await _taken(conn, tenant_id, email, data["userName"])
        await conn.execute(
            insert(EnterpriseUser).values(
                id=user_id,
                tenant_id=tenant_id,
                primary_email=email,
                display_name=display,
                active=active,
                scim_metadata=data,
            )
        )
        await _record(conn, request, user_id, email, None, active)
        row = await _load(conn, tenant_id, user_id)
    body = resource(row, base_url(request))
    return SCIMResponse(body, 201, headers={"Location": body["meta"]["location"]})


@router.get("/Users", summary="List or find people")
async def list_users(
    request: Request, db_engine: AsyncEngine = Depends(get_db_engine)
) -> SCIMResponse:
    tenant_id = request.state.tenant_id
    query = params(request)
    at = page(query)
    where = _u.tenant_id == tenant_id
    found = parse_filter(query.get("filter"), FILTERS, USER)
    if found and found[0] == "userName":
        where &= _user_name() == found[1].lower()
    elif found:
        where &= _u.scim_metadata["externalId"].astext == found[1]
    async with tenant_context(db_engine, tenant_id) as conn:
        total = (await conn.execute(select(func.count()).where(where))).scalar_one()
        rows = (
            await conn.execute(
                select(EnterpriseUser.__table__)
                .where(where)
                .order_by(_u.created_at, _u.id)
                .offset(at.start - 1)
                .limit(at.count)
            )
        ).all()
    base = base_url(request)
    return SCIMResponse(list_response([resource(r, base) for r in rows], total, at))


@router.get("/Users/{id}", summary="Read a person")
async def get_user(
    id: str, request: Request, db_engine: AsyncEngine = Depends(get_db_engine)
) -> SCIMResponse:
    async with tenant_context(db_engine, request.state.tenant_id) as conn:
        row = await _load(conn, request.state.tenant_id, _id(id))
    return SCIMResponse(resource(row, base_url(request)))


@router.put("/Users/{id}", summary="Replace a person")
async def replace_user(
    id: str, request: Request, db_engine: AsyncEngine = Depends(get_db_engine)
) -> SCIMResponse:
    user_id = _id(id)
    data = _tidy(await read_body(request))
    async with tenant_context(db_engine, request.state.tenant_id) as conn:
        row = await _save(conn, request, user_id, data)
    await connection_audiences.resolve_again(db_engine, request.state.tenant_id)
    return SCIMResponse(resource(row, base_url(request)))


@router.patch("/Users/{id}", summary="Change a person")
async def patch_user(
    id: str, request: Request, db_engine: AsyncEngine = Depends(get_db_engine)
) -> SCIMResponse:
    tenant_id = request.state.tenant_id
    user_id = _id(id)
    ops = operations(await read_body(request))
    async with tenant_context(db_engine, tenant_id) as conn:
        current = await _load(conn, tenant_id, user_id, lock=True)
        data = resource(current, "")
        for op in ops:
            apply(data, op.op, op.path, op.value, _name, (ENTERPRISE,))
        row = await _save(conn, request, user_id, _tidy(data))
    await connection_audiences.resolve_again(db_engine, tenant_id)
    return SCIMResponse(resource(row, base_url(request)))


@router.delete("/Users/{id}", status_code=204, summary="Delete a person and what they wrote")
async def delete_user(
    id: str, request: Request, db_engine: AsyncEngine = Depends(get_db_engine)
) -> Response:
    user_id = _id(id)
    ctx = getattr(request.app.state, "ctx", None)
    deleted = await purge.delete_person(
        db_engine,
        request.state.tenant_id,
        actor=request.state.principal.as_actor(audit.SCIM),
        user_id=user_id,
        redis=ctx.redis_client if ctx else None,
    )
    if deleted is None:
        raise not_found("User")
    forget_user(user_id)
    return Response(status_code=204)
