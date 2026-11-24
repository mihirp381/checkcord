"""Audiences an admin sets for connections whose source says nothing about who may read."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from checkcord.models.common import AccessControl
from checkcord.models.enums import AccessVisibility, AclType
from checkcord.storage import audiences as store
from checkcord.storage import audit
from checkcord.storage.models import (
    ConnectionAudience,
    EnterpriseUser,
    ScimGroup,
    ScimGroupMember,
)
from checkcord.storage.postgres import events_table, tenant_context

logger = structlog.get_logger(__name__).bind(component="connection_audiences")

NOBODY, EVERYONE, GROUPS, PEOPLE = "nobody", "everyone", "groups", "people"
MODES = (NOBODY, EVERYONE, GROUPS, PEOPLE)
BATCH_SIZE = 500

_t = ConnectionAudience.__table__
_c = _t.c
_ev = events_table.c
_eu = EnterpriseUser.__table__.c
_g = ScimGroup.__table__.c
_gm = ScimGroupMember.__table__.c


def admin_providers() -> set[str]:
    from checkcord import connectors

    return {c.name for c in connectors.connectors() if c.audience_from == "admin"}


def provider_of(connection: str) -> str:
    return connection.partition(":")[0]


def container_of(value: str | None) -> str | None:
    from checkcord.services import scope

    return scope.normalize(value) if value and value.strip() else None


@dataclass(frozen=True)
class Setting:
    connection: str
    container: str | None
    mode: str
    groups: tuple[str, ...] = ()
    people: tuple[str, ...] = ()
    member_ids: tuple[UUID, ...] = ()

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Setting:
        return cls(
            row["connection"],
            row["container"],
            row["mode"],
            tuple(row["groups"] or ()),
            tuple(row["people"] or ()),
            tuple(row["member_ids"] or ()),
        )

    @property
    def audience(self) -> store.Audience:
        if self.mode == EVERYONE:
            return store.Audience(True)
        if self.mode == NOBODY:
            return store.EMPTY
        return store.Audience.of(False, self.member_ids)

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "groups": list(self.groups),
            "people": list(self.people),
            "members": len(self.member_ids),
        }


def shape(audience: store.Audience) -> AccessControl:
    if audience.is_public:
        return AccessControl(
            is_public=True, visibility=AccessVisibility.PUBLIC, acl_type=AclType.OPEN
        )
    return AccessControl(
        is_public=False,
        visibility=AccessVisibility.PRIVATE,
        acl_type=AclType.CUSTOM,
        allowed_identity_ids=sorted(UUID(m) for m in audience.member_ids),
    )


def pick(settings: Iterable[Setting], container: str | None) -> Setting | None:
    """The container's own setting, else the connection's."""
    found = {s.container: s for s in settings}
    return (found.get(container) if container else None) or found.get(None)


async def _members(
    conn: AsyncConnection, tenant_id: UUID, mode: str, groups: Iterable[str], people: Iterable[str]
) -> list[UUID]:
    if mode == GROUPS:
        names = sorted({g.strip().lower() for g in groups})
        stmt = (
            select(_gm.user_id)
            .select_from(
                ScimGroupMember.__table__.join(ScimGroup.__table__, _g.id == _gm.group_id).join(
                    EnterpriseUser.__table__, _eu.id == _gm.user_id
                )
            )
            .where(
                (_g.tenant_id == tenant_id) & func.lower(_g.display_name).in_(names) & _eu.active
            )
        )
    elif mode == PEOPLE:
        emails = sorted({p.strip().lower() for p in people})
        stmt = select(_eu.id).where(
            (_eu.tenant_id == tenant_id) & func.lower(_eu.primary_email).in_(emails) & _eu.active
        )
    else:
        return []
    return sorted({r[0] for r in await conn.execute(stmt)}, key=str)


async def _known(conn: AsyncConnection, tenant_id: UUID, mode: str, names: list[str]) -> set[str]:
    if mode == GROUPS:
        rows = await conn.execute(
            select(func.lower(_g.display_name)).where(
                (_g.tenant_id == tenant_id) & func.lower(_g.display_name).in_(names)
            )
        )
    else:
        rows = await conn.execute(
            select(func.lower(_eu.primary_email)).where(
                (_eu.tenant_id == tenant_id) & func.lower(_eu.primary_email).in_(names)
            )
        )
    return {r[0] for r in rows}


async def settings_of(
    conn: AsyncConnection, tenant_id: UUID, connection: str | None = None, *, lock: bool = False
) -> list[Setting]:
    stmt = select(_t).where(_c.tenant_id == tenant_id)
    if connection is not None:
        stmt = stmt.where(_c.connection == connection)
    if lock:
        stmt = stmt.with_for_update(read=True)
    return [
        Setting.from_row(r)
        for r in (await conn.execute(stmt.order_by(_c.connection, _c.container))).mappings()
    ]


async def apply_at_ingest(conn: AsyncConnection, rows: list[dict[str, Any]]) -> None:
    """Give events of admin-audience connections the audience set for them; nobody when unset."""
    admin = admin_providers()
    for row in rows:
        key = row.get("connection")
        if not key or provider_of(str(key)) not in admin:
            continue
        found = pick(
            await settings_of(conn, row["tenant_id"], str(key), lock=True),
            row.get("audience_container"),
        )
        shaped = shape(found.audience if found else store.EMPTY)
        row["access_control"] = {
            **row["access_control"],
            "is_public": shaped.is_public,
            "visibility": shaped.visibility.value,
            "acl_type": shaped.acl_type.value,
        }
        row["access_allowed_ids"] = list(shaped.allowed_identity_ids)


async def list_settings(engine: AsyncEngine, tenant_id: UUID) -> list[dict[str, Any]]:
    async with tenant_context(engine, tenant_id) as conn:
        rows = (await conn.execute(select(_t).where(_c.tenant_id == tenant_id))).mappings().all()
    return [
        {
            **Setting.from_row(r).describe(),
            "connection": r["connection"],
            "container": r["container"],
            "updated_by": r["updated_by"],
            "updated_at": r["updated_at"],
            "recompute": {
                "state": r["recompute_state"],
                "done": r["recompute_done"],
                "total": r["recompute_total"],
                "finished_at": r["recomputed_at"],
            },
        }
        for r in sorted(rows, key=lambda r: (r["connection"], r["container"] or ""))
    ]


async def set_audience(
    engine: AsyncEngine,
    tenant_id: UUID,
    connection: str,
    mode: str,
    actor: audit.Actor,
    *,
    container: str | None = None,
    groups: Iterable[str] = (),
    people: Iterable[str] = (),
) -> Setting:
    """Store who may read a connection (or one of its containers). Run ``recompute`` after it
    to bring what was already ingested in line."""
    from checkcord.services.connections import connection_key, list_connections

    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if provider_of(connection) not in admin_providers():
        raise ValueError(
            f"{connection} takes its audience from its source; only "
            f"{', '.join(sorted(admin_providers()))} connections take one from an admin"
        )
    known = {
        connection_key(c.provider, c.identifier) for c in await list_connections(engine, tenant_id)
    }
    if connection not in known:
        raise LookupError(f"no connection {connection!r}")
    container = container_of(container)
    names = sorted({n.strip().lower() for n in (groups if mode == GROUPS else people) if n.strip()})
    if mode in (GROUPS, PEOPLE) and not names:
        raise ValueError(f"name at least one {'group' if mode == GROUPS else 'person'}")
    if mode not in (GROUPS, PEOPLE):
        names = []
    async with tenant_context(engine, tenant_id) as conn:
        missing = sorted(set(names) - await _known(conn, tenant_id, mode, names)) if names else []
        if missing:
            kind = "group" if mode == GROUPS else "person"
            raise LookupError(f"no {kind} {', '.join(missing)}")
        where = (
            (_c.tenant_id == tenant_id)
            & (_c.connection == connection)
            & (func.coalesce(_c.container, "") == (container or ""))
        )
        row = (await conn.execute(select(_t).where(where).with_for_update())).mappings().first()
        before = Setting.from_row(row) if row else None
        members = await _members(conn, tenant_id, mode, names, names)
        values = {
            "mode": mode,
            "groups": names if mode == GROUPS else [],
            "people": names if mode == PEOPLE else [],
            "member_ids": members,
            "updated_by": actor.user_email or actor.key_name or actor.channel,
            "updated_at": func.now(),
            "recompute_state": "pending",
            "recompute_done": 0,
            "recompute_total": 0,
        }
        if row is None:
            await conn.execute(
                pg_insert(_t).values(
                    tenant_id=tenant_id, connection=connection, container=container, **values
                )
            )
        else:
            await conn.execute(update(_t).where(_c.id == row["id"]).values(**values))
        after = Setting(
            connection,
            container,
            mode,
            tuple(values["groups"]),
            tuple(values["people"]),
            tuple(members),
        )
        await audit.record(
            conn,
            tenant_id,
            actor,
            "connection.audience_changed",
            params={
                "connection": connection,
                "container": container,
                "before": before.describe() if before else {"mode": NOBODY},
                "after": after.describe(),
            },
        )
    return after


def _events(tenant_id: UUID, setting: Setting, others: Iterable[str]) -> Any:
    where = (_ev.tenant_id == tenant_id) & (_ev.connection == setting.connection)
    if setting.container is not None:
        return where & (_ev.audience_container == setting.container)
    others = sorted(others)
    if others:
        where = where & (_ev.audience_container.is_(None) | _ev.audience_container.not_in(others))
    return where


async def recompute(
    engine: AsyncEngine,
    tenant_id: UUID,
    connection: str,
    container: str | None = None,
    *,
    batch_size: int = BATCH_SIZE,
    progress: Callable[[int, int], None] | None = None,
    redis: Redis | None = None,
) -> int:
    """Bring a setting's events, and the records based on them, in line with it, in batches.
    Each batch reads the setting again, so a later change wins. Returns events changed."""
    from checkcord.services import audiences

    container = container_of(container)
    where = (
        (_c.tenant_id == tenant_id)
        & (_c.connection == connection)
        & (func.coalesce(_c.container, "") == (container or ""))
    )
    applied = store.Applied(None, store.EMPTY)
    total = 0
    first = True
    while True:
        async with tenant_context(
            engine, tenant_id, include_held=True, include_retired=True
        ) as conn:
            settings = await settings_of(conn, tenant_id, connection)
            setting = next((s for s in settings if s.container == container), None)
            if setting is None:
                return applied.events
            others = [s.container for s in settings if s.container is not None]
            events = _events(tenant_id, setting, others)
            target = setting.audience
            if first:
                total = (
                    await conn.execute(select(func.count()).where(events & store.differs(target)))
                ).scalar_one()
                await conn.execute(
                    update(_t)
                    .where(where)
                    .values(recompute_state="running", recompute_done=0, recompute_total=total)
                )
                first = False
            done = await store.batch(
                conn,
                tenant_id,
                provider_of(connection),
                container or "",
                target,
                narrow_only=False,
                limit=batch_size,
                applied=applied,
                events=events,
                shape=shape,
            )
            finished = done < batch_size
            await conn.execute(
                update(_t)
                .where(where)
                .values(
                    recompute_done=applied.events,
                    recompute_total=max(total, applied.events),
                    **(
                        {"recompute_state": "done", "recomputed_at": datetime.now(UTC)}
                        if finished
                        else {}
                    ),
                )
            )
        if progress is not None:
            progress(applied.events, max(total, applied.events))
        if finished:
            break
    if applied.narrowed_records:
        await audiences.rewrite_files(
            redis,
            tenant_id,
            provider_of(connection),
            container or connection,
            applied.narrowed_records,
        )
    logger.info(
        "connection_audiences.recomputed",
        connection=connection,
        container=container,
        events=applied.events,
        records=applied.records,
    )
    return applied.events


async def resolve_again(
    engine: AsyncEngine, tenant_id: UUID, redis: Redis | None = None
) -> list[tuple[str, str | None]]:
    """Read group and people settings against the directory again and recompute those whose
    members changed. Returns the settings that changed."""
    changed: list[tuple[str, str | None]] = []
    async with tenant_context(engine, tenant_id) as conn:
        rows = (
            (
                await conn.execute(
                    select(_t)
                    .where((_c.tenant_id == tenant_id) & _c.mode.in_((GROUPS, PEOPLE)))
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        for r in rows:
            members = await _members(conn, tenant_id, r["mode"], r["groups"], r["people"])
            if set(members) != set(r["member_ids"] or ()):
                await conn.execute(
                    update(_t)
                    .where(_c.id == r["id"])
                    .values(member_ids=members, recompute_state="pending")
                )
                changed.append((r["connection"], r["container"]))
    for connection, container in changed:
        try:
            await recompute(engine, tenant_id, connection, container, redis=redis)
        except Exception:
            logger.exception(
                "connection_audiences.recompute_failed", connection=connection, container=container
            )
    return changed


_NOBODY = """
SELECT e.connection, count(DISTINCT c.id) FROM canonical_contexts c
JOIN events e ON e.tenant_id = c.tenant_id AND e.event_id = c.base_event_id
WHERE c.tenant_id = :tenant AND e.connection IS NOT NULL
  AND coalesce((c.access_control->>'is_public')::boolean, false) = false
  AND jsonb_array_length(coalesce(c.access_control->'allowed_identity_ids', '[]'::jsonb)) = 0
  AND jsonb_array_length(coalesce(c.access_control->'admin_overrides', '[]'::jsonb)) = 0
GROUP BY e.connection
"""


async def nobody_counts(engine: AsyncEngine, tenant_id: UUID) -> dict[str, int]:
    """Live records nobody may read, based on an event of an admin-audience connection, per
    connection."""
    admin = admin_providers()
    async with tenant_context(engine, tenant_id) as conn:
        rows = await conn.execute(text(_NOBODY), {"tenant": str(tenant_id)})
        return {r[0]: int(r[1]) for r in rows if provider_of(r[0]) in admin}
