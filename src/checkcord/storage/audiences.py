"""Stored audiences of containers (Slack channels, Teams conversations, GitHub repositories)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, bindparam, cast, func, literal, not_, select, update
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from checkcord.models.common import AccessControl
from checkcord.models.enums import AccessVisibility, AclType
from checkcord.storage.models import AudienceContainer, CanonicalContext, ContextEventLink
from checkcord.storage.postgres import PostgresEventStore, events_table, tenant_context

_t = AudienceContainer.__table__
_c = _t.c
_ev = events_table.c
_UUIDS = ARRAY(PG_UUID(as_uuid=True))
_ACL_TYPES = {"github": AclType.REPO_COLLABORATORS}
UNMATCHED_KEPT = 200


@dataclass(frozen=True)
class Audience:
    is_public: bool
    member_ids: frozenset[str] = frozenset()

    @classmethod
    def of(cls, is_public: bool | None, member_ids: Any = ()) -> Audience:
        if is_public is True:
            return cls(True)
        return cls(False, frozenset(str(m) for m in member_ids or ()))

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Audience:
        return cls.of(row["is_public"], row["member_ids"])

    def within(self, other: Audience) -> Audience:
        """Readers allowed by both."""
        if other.is_public:
            return self
        if self.is_public:
            return other
        return Audience(False, self.member_ids & other.member_ids)

    def access_control(self, platform: str, container: str) -> AccessControl:
        if self.is_public:
            return AccessControl(
                is_public=True,
                visibility=AccessVisibility.PUBLIC,
                acl_type=AclType.OPEN,
                source_group_id=container,
            )
        return AccessControl(
            is_public=False,
            visibility=AccessVisibility.PRIVATE,
            acl_type=_ACL_TYPES.get(platform, AclType.CHANNEL_MEMBERS),
            allowed_identity_ids=sorted(UUID(m) for m in self.member_ids),
            source_group_id=container,
        )


EMPTY = Audience(False)


@dataclass
class Applied:
    before: Audience | None
    after: Audience
    kept: bool = False
    superseded: bool = False
    events: int = 0
    records: int = 0
    narrowed_records: list[UUID] = field(default_factory=list)
    unmatched: int = 0

    @property
    def changed(self) -> bool:
        return self.before is not None and self.before != self.after


def _key(tenant_id: UUID, platform: str, container: str) -> Any:
    return (_c.tenant_id == tenant_id) & (_c.platform == platform) & (_c.container == container)


def _events_of(tenant_id: UUID, platform: str, container: str) -> Any:
    return (
        (_ev.tenant_id == tenant_id)
        & (_ev.source_platform == platform)
        & (_ev.audience_container == container)
    )


def _event_is_public() -> Any:
    return func.coalesce(cast(_ev.access_control["is_public"].astext, Boolean), False)


def _ids(audience: Audience) -> Any:
    return cast(literal(sorted(UUID(m) for m in audience.member_ids), _UUIDS), _UUIDS)


def differs(target: Audience) -> Any:
    if target.is_public:
        return not_(_event_is_public()) | (func.cardinality(_ev.access_allowed_ids) > 0)
    ids = _ids(target)
    return _event_is_public() | not_(
        _ev.access_allowed_ids.contains(ids) & _ev.access_allowed_ids.contained_by(ids)
    )


def _wider(target: Audience) -> Any:
    return _event_is_public() | not_(_ev.access_allowed_ids.contained_by(_ids(target)))


def _narrowed(old: Mapping[str, Any] | None, new: Mapping[str, Any] | None) -> bool:
    if new is None or new.get("is_public") is True:
        return False
    old = old or {}
    if old.get("is_public") is True:
        return True
    return bool(
        {str(u) for u in old.get("allowed_identity_ids") or []}
        - {str(u) for u in new.get("allowed_identity_ids") or []}
    )


async def get(
    conn: AsyncConnection, tenant_id: UUID, platform: str, container: str
) -> dict[str, Any] | None:
    row = (
        (await conn.execute(select(_t).where(_key(tenant_id, platform, container))))
        .mappings()
        .one_or_none()
    )
    return dict(row) if row else None


async def list_rows(conn: AsyncConnection, tenant_id: UUID) -> list[dict[str, Any]]:
    stmt = select(_t).where(_c.tenant_id == tenant_id).order_by(_c.platform, _c.container)
    return [dict(r) for r in (await conn.execute(stmt)).mappings()]


def is_fresh(row: Mapping[str, Any], now: datetime, interval: timedelta, grace: timedelta) -> bool:
    attempted, refreshed = row.get("attempted_at"), row.get("refreshed_at")
    if attempted is None or attempted <= now - interval:
        return False
    return refreshed is None or refreshed > now - grace


async def batch(
    conn: AsyncConnection,
    tenant_id: UUID,
    platform: str,
    container: str,
    target: Audience,
    *,
    narrow_only: bool,
    limit: int,
    applied: Applied,
    events: Any = None,
    shape: Callable[[Audience], AccessControl] | None = None,
) -> int:
    condition = _wider(target) if narrow_only else differs(target)
    if events is None:
        events = _events_of(tenant_id, platform, container)
    rows = (
        (
            await conn.execute(
                select(_ev.event_id, _ev.happened_at, _ev.access_control, _ev.access_allowed_ids)
                .where(events & condition)
                .order_by(_ev.event_id)
                .limit(limit)
                .with_for_update()
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        return 0

    updates = []
    for row in rows:
        ac = dict(row["access_control"] or {})
        new = target
        if narrow_only:
            new = Audience.of(ac.get("is_public"), row["access_allowed_ids"]).within(target)
        shaped = shape(new) if shape else new.access_control(platform, container)
        ac.update(
            is_public=shaped.is_public,
            visibility=shaped.visibility.value,
            acl_type=shaped.acl_type.value,
        )
        updates.append(
            {
                "e_id": row["event_id"],
                "e_at": row["happened_at"],
                "ac": ac,
                "ids": sorted(UUID(m) for m in new.member_ids),
            }
        )
    await conn.execute(
        update(events_table)
        .where(
            (_ev.tenant_id == tenant_id)
            & (_ev.event_id == bindparam("e_id"))
            & (_ev.happened_at == bindparam("e_at"))
        )
        .values(access_control=bindparam("ac"), access_allowed_ids=bindparam("ids")),
        updates,
    )
    applied.events += len(rows)

    links = ContextEventLink.__table__
    contexts = CanonicalContext.__table__
    linked = (
        await conn.execute(
            select(contexts.c.id, contexts.c.access_control)
            .select_from(links.join(contexts, links.c.canonical_id == contexts.c.id))
            .where(
                (links.c.tenant_id == tenant_id)
                & (contexts.c.tenant_id == tenant_id)
                & links.c.event_id.in_([u["e_id"] for u in updates])
            )
            .distinct()
            .order_by(contexts.c.id)
        )
    ).all()
    store = PostgresEventStore(conn)
    for record_id, old in linked:
        new_ac = await store.recompute_context_access_control(tenant_id, record_id)
        if new_ac is None:
            continue
        applied.records += 1
        if _narrowed(old, new_ac):
            applied.narrowed_records.append(record_id)
    return len(rows)


async def apply(
    engine: AsyncEngine,
    tenant_id: UUID,
    platform: str,
    container: str,
    target: Audience | None,
    *,
    now: datetime,
    grace: timedelta,
    batch_size: int,
    error: str | None = None,
    details: Mapping[str, Any] | None = None,
    unmatched: list[str] | None = None,
) -> Applied:
    """Store a container's audience and bring its events and their records in line.

    ``unmatched`` lists the members not matched to a person; None keeps what is stored.
    ``target`` None means the provider could not be read: the stored audience is kept
    within ``grace`` of the last good read, and is empty after it. A change that touches
    more than ``batch_size`` events is applied in batches, narrowing every event first.
    """
    async with tenant_context(engine, tenant_id, include_held=True, include_retired=True) as conn:
        inserted = (
            await conn.execute(
                insert(_t)
                .values(
                    tenant_id=tenant_id,
                    platform=platform,
                    container=container,
                    details=dict(details or {}),
                    created_at=now,
                )
                .on_conflict_do_nothing(constraint="uq_audience_containers_container")
                .returning(_c.id)
            )
        ).first() is not None
        row = (
            (
                await conn.execute(
                    select(_t).where(_key(tenant_id, platform, container)).with_for_update()
                )
            )
            .mappings()
            .one()
        )
        before = None if inserted else Audience.from_row(row)
        values: dict[str, Any] = {"attempted_at": now, "last_error": error}
        if details:
            values["details"] = {**(row["details"] or {}), **details}
        if unmatched is not None:
            values.update(
                unmatched_count=len(unmatched),
                unmatched_accounts=sorted(unmatched)[:UNMATCHED_KEPT],
            )
        if target is None:
            since = row["refreshed_at"] or row["created_at"]
            if not inserted and since > now - grace:
                await conn.execute(update(_t).where(_c.id == row["id"]).values(**values))
                return Applied(before, before or EMPTY, kept=True, unmatched=row["unmatched_count"])
            target = EMPTY
        else:
            values["refreshed_at"] = now
        values.update(
            is_public=target.is_public, member_ids=sorted(UUID(m) for m in target.member_ids)
        )
        await conn.execute(update(_t).where(_c.id == row["id"]).values(**values))

        applied = Applied(
            before,
            target,
            unmatched=len(unmatched) if unmatched is not None else row["unmatched_count"],
        )
        pending = (
            await conn.execute(
                select(func.count()).select_from(
                    select(_ev.event_id)
                    .where(_events_of(tenant_id, platform, container) & differs(target))
                    .limit(batch_size + 1)
                    .subquery()
                )
            )
        ).scalar_one()
        if pending <= batch_size:
            await batch(
                conn,
                tenant_id,
                platform,
                container,
                target,
                narrow_only=False,
                limit=batch_size,
                applied=applied,
            )
            return applied

    for narrow_only in (True, False):
        if narrow_only and target.is_public:
            continue
        while True:
            async with tenant_context(
                engine, tenant_id, include_held=True, include_retired=True
            ) as conn:
                current = (
                    (
                        await conn.execute(
                            select(_c.is_public, _c.member_ids)
                            .where(_key(tenant_id, platform, container))
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                # A later refresh stored a different audience and now owns the events.
                if current is None or Audience.from_row(current) != target:
                    applied.superseded = True
                    return applied
                done = await batch(
                    conn,
                    tenant_id,
                    platform,
                    container,
                    target,
                    narrow_only=narrow_only,
                    limit=batch_size,
                    applied=applied,
                )
            if done < batch_size:
                break
    return applied
