"""SCIM Groups. Members of a group named in SCIM_ADMIN_GROUPS are admins."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from checkcord.api.deps import get_db_engine
from checkcord.core.config import get_settings
from checkcord.scim.protocol import (
    GROUP,
    SCIMException,
    SCIMResponse,
    base_url,
    canon,
    get_ci,
    list_response,
    not_found,
    operations,
    page,
    params,
    parse_filter,
    parse_path,
    read_body,
    strip_urn,
)
from checkcord.services import connection_audiences, people
from checkcord.storage import audit
from checkcord.storage.models import EnterpriseUser, ScimGroup, ScimGroupMember
from checkcord.storage.postgres import tenant_context

router = APIRouter()

_g = ScimGroup.__table__.c
_m = ScimGroupMember.__table__.c
_u = EnterpriseUser.__table__.c
NAMES = {"displayname": "displayName", "externalid": "externalId", "members": "members"}
FILTERS = {"displayname": "displayName", "externalid": "externalId"}


@dataclass
class State:
    name: str
    external_id: str | None
    members: set[UUID] = field(default_factory=set)
    ignored: int = 0


def _id(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError as err:
        raise not_found("Group") from err


def _values(value: Any) -> list[str]:
    items = value if isinstance(value, list) else [value]
    out = []
    for item in items:
        member = get_ci(item, "value") if isinstance(item, dict) else None
        if not isinstance(member, str):
            raise SCIMException(400, "Each member needs a value, the person's id", "invalidValue")
        out.append(member)
    return out


async def _known(
    conn: AsyncConnection, tenant_id: UUID, state: State, values: Iterable[str]
) -> set[UUID]:
    wanted, ignored = set(), 0
    for raw in values:
        try:
            wanted.add(UUID(raw))
        except ValueError:
            ignored += 1
    found = set()
    if wanted:
        rows = await conn.execute(
            select(_u.id).where((_u.tenant_id == tenant_id) & _u.id.in_(wanted))
        )
        found = {r.id for r in rows}
    state.ignored += ignored + len(wanted - found)
    return found


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SCIMException(400, "displayName is required", "invalidValue")
    return value.strip()


def _external(value: Any) -> str | None:
    if value is not None and not isinstance(value, str):
        raise SCIMException(400, "externalId must be a string", "invalidValue")
    return value or None


async def _from_body(conn: AsyncConnection, tenant_id: UUID, body: dict[str, Any]) -> State:
    data = canon(body, NAMES)
    state = State(_name(data.get("displayName")), _external(data.get("externalId")))
    state.members = await _known(conn, tenant_id, state, _values(data.get("members") or []))
    return state


async def _members(conn: AsyncConnection, tenant_id: UUID, group_id: UUID) -> list[Any]:
    rows = await conn.execute(
        select(_u.id, _u.display_name, _u.primary_email)
        .join(ScimGroupMember.__table__, _m.user_id == _u.id)
        .where((_m.tenant_id == tenant_id) & (_m.group_id == group_id))
        .order_by(_u.primary_email)
    )
    return list(rows.all())


def _resource(row: Any, members: list[Any] | None, base: str) -> dict[str, Any]:
    out: dict[str, Any] = {"schemas": [GROUP], "id": str(row.id), "displayName": row.display_name}
    if row.external_id:
        out["externalId"] = row.external_id
    if members is not None:
        out["members"] = [
            {
                "value": str(m.id),
                "display": m.display_name or m.primary_email,
                "$ref": f"{base}/Users/{m.id}",
                "type": "User",
            }
            for m in members
        ]
    out["meta"] = {
        "resourceType": "Group",
        "created": row.created_at.isoformat(),
        "lastModified": row.updated_at.isoformat(),
        "location": f"{base}/Groups/{row.id}",
    }
    return out


def _with_members(query: dict[str, str]) -> bool:
    excluded = {a.strip().lower() for a in query.get("excludedattributes", "").split(",")}
    return "members" not in excluded


async def _load(conn: AsyncConnection, tenant_id: UUID, group_id: UUID, lock: bool = False) -> Any:
    query = select(ScimGroup.__table__).where((_g.tenant_id == tenant_id) & (_g.id == group_id))
    row = (await conn.execute(query.with_for_update() if lock else query)).first()
    if row is None:
        raise not_found("Group")
    return row


async def _name_free(
    conn: AsyncConnection, tenant_id: UUID, name: str, but: UUID | None = None
) -> None:
    where = (_g.tenant_id == tenant_id) & (func.lower(_g.display_name) == name.lower())
    if but is not None:
        where &= _g.id != but
    if (await conn.execute(select(_g.id).where(where).limit(1))).first():
        raise SCIMException(409, f"A group named {name} exists", "uniqueness")


def _admin_names() -> set[str]:
    return {n.strip().lower() for n in get_settings().scim_admin_groups if n.strip()}


async def sync_admins(
    conn: AsyncConnection, tenant_id: UUID, user_ids: set[UUID], actor: audit.Actor
) -> set[UUID]:
    """With SCIM_ADMIN_GROUPS set, a person is an admin exactly when they are in a listed group."""
    names = _admin_names()
    if not names or not user_ids:
        return set()
    rows = await conn.execute(
        select(_m.user_id)
        .join(ScimGroup.__table__, _g.id == _m.group_id)
        .where(
            (_m.tenant_id == tenant_id)
            & _m.user_id.in_(user_ids)
            & func.lower(_g.display_name).in_(names)
        )
    )
    admins = {r.user_id for r in rows}
    for user_id in sorted(user_ids):
        await people.apply_admin(conn, tenant_id, user_id, user_id in admins, actor, "scim_group")
    return admins


async def _write(
    conn: AsyncConnection,
    request: Request,
    group_id: UUID,
    before: State | None,
    after: State,
) -> None:
    tenant_id = request.state.tenant_id
    actor = request.state.principal.as_actor(audit.SCIM)
    old = before.members if before else set()
    added, removed = after.members - old, old - after.members
    if removed:
        await conn.execute(
            delete(ScimGroupMember).where(
                (_m.tenant_id == tenant_id) & (_m.group_id == group_id) & _m.user_id.in_(removed)
            )
        )
    if added:
        await conn.execute(
            insert(ScimGroupMember),
            [{"tenant_id": tenant_id, "group_id": group_id, "user_id": u} for u in added],
        )
    await audit.record(
        conn,
        tenant_id,
        actor,
        "group.created" if before is None else "group.updated",
        params={
            "display_name": after.name,
            "renamed_from": before.name if before and before.name != after.name else None,
            "added": len(added),
            "removed": len(removed),
            "ignored_members": after.ignored or None,
        },
        record_ids=[group_id],
    )
    renamed = before is not None and before.name.lower() != after.name.lower()
    await sync_admins(
        conn, tenant_id, (old | after.members) if renamed else (added | removed), actor
    )


async def _state(conn: AsyncConnection, tenant_id: UUID, row: Any) -> State:
    rows = await conn.execute(
        select(_m.user_id).where((_m.tenant_id == tenant_id) & (_m.group_id == row.id))
    )
    return State(row.display_name, row.external_id, {r.user_id for r in rows})


async def _respond(
    conn: AsyncConnection, request: Request, group_id: UUID, status: int = 200
) -> SCIMResponse:
    tenant_id = request.state.tenant_id
    row = await _load(conn, tenant_id, group_id)
    body = _resource(row, await _members(conn, tenant_id, group_id), base_url(request))
    headers = {"Location": body["meta"]["location"]} if status == 201 else None
    return SCIMResponse(body, status, headers=headers)


async def _members_changed(request: Request, db_engine: AsyncEngine) -> None:
    """Connection audiences set by group follow the group."""
    ctx = getattr(request.app.state, "ctx", None)
    await connection_audiences.resolve_again(
        db_engine, request.state.tenant_id, ctx.redis_client if ctx else None
    )


@router.post("/Groups", status_code=201, summary="Create a group")
async def create_group(
    request: Request, db_engine: AsyncEngine = Depends(get_db_engine)
) -> SCIMResponse:
    tenant_id = request.state.tenant_id
    body = await read_body(request)
    group_id = uuid4()
    async with tenant_context(db_engine, tenant_id) as conn:
        state = await _from_body(conn, tenant_id, body)
        await _name_free(conn, tenant_id, state.name)
        await conn.execute(
            insert(ScimGroup).values(
                id=group_id,
                tenant_id=tenant_id,
                display_name=state.name,
                external_id=state.external_id,
            )
        )
        await _write(conn, request, group_id, None, state)
        response = await _respond(conn, request, group_id, 201)
    await _members_changed(request, db_engine)
    return response


@router.get("/Groups", summary="List or find groups")
async def list_groups(
    request: Request, db_engine: AsyncEngine = Depends(get_db_engine)
) -> SCIMResponse:
    tenant_id = request.state.tenant_id
    query = params(request)
    at = page(query)
    where = _g.tenant_id == tenant_id
    found = parse_filter(query.get("filter"), FILTERS, GROUP)
    if found and found[0] == "displayName":
        where &= func.lower(_g.display_name) == found[1].lower()
    elif found:
        where &= _g.external_id == found[1]
    base = base_url(request)
    async with tenant_context(db_engine, tenant_id) as conn:
        total = (await conn.execute(select(func.count()).where(where))).scalar_one()
        rows = (
            await conn.execute(
                select(ScimGroup.__table__)
                .where(where)
                .order_by(_g.created_at, _g.id)
                .offset(at.start - 1)
                .limit(at.count)
            )
        ).all()
        resources = [
            _resource(
                r,
                await _members(conn, tenant_id, r.id) if _with_members(query) else None,
                base,
            )
            for r in rows
        ]
    return SCIMResponse(list_response(resources, total, at))


@router.get("/Groups/{id}", summary="Read a group")
async def get_group(
    id: str, request: Request, db_engine: AsyncEngine = Depends(get_db_engine)
) -> SCIMResponse:
    tenant_id = request.state.tenant_id
    group_id = _id(id)
    async with tenant_context(db_engine, tenant_id) as conn:
        row = await _load(conn, tenant_id, group_id)
        members = await _members(conn, tenant_id, group_id)
    shown = members if _with_members(params(request)) else None
    return SCIMResponse(_resource(row, shown, base_url(request)))


async def _rename(conn: AsyncConnection, tenant_id: UUID, group_id: UUID, state: State) -> None:
    await _name_free(conn, tenant_id, state.name, but=group_id)
    await conn.execute(
        update(ScimGroup)
        .where((_g.tenant_id == tenant_id) & (_g.id == group_id))
        .values(display_name=state.name, external_id=state.external_id, updated_at=func.now())
    )


@router.put("/Groups/{id}", summary="Replace a group")
async def replace_group(
    id: str, request: Request, db_engine: AsyncEngine = Depends(get_db_engine)
) -> SCIMResponse:
    tenant_id = request.state.tenant_id
    group_id = _id(id)
    body = await read_body(request)
    async with tenant_context(db_engine, tenant_id) as conn:
        before = await _state(conn, tenant_id, await _load(conn, tenant_id, group_id, lock=True))
        after = await _from_body(conn, tenant_id, body)
        await _rename(conn, tenant_id, group_id, after)
        await _write(conn, request, group_id, before, after)
        response = await _respond(conn, request, group_id)
    await _members_changed(request, db_engine)
    return response


async def _patch_members(
    conn: AsyncConnection,
    tenant_id: UUID,
    state: State,
    op: str,
    value: Any,
    only: str | None = None,
) -> None:
    if op == "remove":
        if only is not None:
            gone = {only}
        elif value is None:
            state.members = set()
            return
        else:
            gone = set(_values(value))
        lowered = {g.strip().lower() for g in gone}
        state.members = {m for m in state.members if str(m) not in lowered}
        return
    values = [only] if only is not None else _values(value)
    found = await _known(conn, tenant_id, state, values)
    state.members = found if op == "replace" and only is None else state.members | found


async def _patch_one(
    conn: AsyncConnection, tenant_id: UUID, state: State, op: str, attr: str, value: Any
) -> None:
    if attr == "displayname":
        if op == "remove":
            raise SCIMException(400, "displayName cannot be removed", "mutability")
        state.name = _name(value)
    elif attr == "externalid":
        state.external_id = None if op == "remove" else _external(value)
    elif attr == "members":
        await _patch_members(conn, tenant_id, state, op, value)
    elif attr not in ("id", "schemas", "meta"):
        raise SCIMException(400, f"Groups have no attribute {attr}", "invalidPath")


@router.patch("/Groups/{id}", status_code=204, summary="Change a group or its members")
async def patch_group(
    id: str, request: Request, db_engine: AsyncEngine = Depends(get_db_engine)
) -> Response:
    tenant_id = request.state.tenant_id
    group_id = _id(id)
    ops = operations(await read_body(request))
    async with tenant_context(db_engine, tenant_id) as conn:
        before = await _state(conn, tenant_id, await _load(conn, tenant_id, group_id, lock=True))
        after = State(before.name, before.external_id, set(before.members))
        for op in ops:
            if op.path is None:
                if op.op == "remove" or not isinstance(op.value, dict):
                    raise SCIMException(
                        400, "An operation without a path needs an object", "noTarget"
                    )
                for key, value in op.value.items():
                    await _patch_one(conn, tenant_id, after, op.op, key.lower(), value)
                continue
            target = parse_path(strip_urn(op.path, GROUP))
            attr = target.attr.lower()
            if target.where is None and target.sub is None:
                await _patch_one(conn, tenant_id, after, op.op, attr, op.value)
            elif attr == "members" and target.where and target.where[0].lower() == "value":
                await _patch_members(conn, tenant_id, after, op.op, op.value, target.where[1])
            else:
                raise SCIMException(400, f"Cannot change {op.path}", "invalidPath")
        if (after.name, after.external_id) != (before.name, before.external_id):
            await _rename(conn, tenant_id, group_id, after)
        else:
            await conn.execute(
                update(ScimGroup)
                .where((_g.tenant_id == tenant_id) & (_g.id == group_id))
                .values(updated_at=func.now())
            )
        await _write(conn, request, group_id, before, after)
    await _members_changed(request, db_engine)
    return Response(status_code=204)


@router.delete("/Groups/{id}", status_code=204, summary="Delete a group")
async def delete_group(
    id: str, request: Request, db_engine: AsyncEngine = Depends(get_db_engine)
) -> Response:
    tenant_id = request.state.tenant_id
    group_id = _id(id)
    actor = request.state.principal.as_actor(audit.SCIM)
    async with tenant_context(db_engine, tenant_id) as conn:
        state = await _state(conn, tenant_id, await _load(conn, tenant_id, group_id, lock=True))
        await conn.execute(
            delete(ScimGroup).where((_g.tenant_id == tenant_id) & (_g.id == group_id))
        )
        await audit.record(
            conn,
            tenant_id,
            actor,
            "group.deleted",
            params={"display_name": state.name, "members": len(state.members)},
            record_ids=[group_id],
        )
        await sync_admins(conn, tenant_id, state.members, actor)
    await _members_changed(request, db_engine)
    return Response(status_code=204)
