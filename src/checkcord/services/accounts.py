"""Slack, Teams and GitHub accounts, and the person each is matched to."""

from __future__ import annotations

import csv
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy import and_, func, literal, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from checkcord.storage import audit
from checkcord.storage.models import AudienceContainer, ChatUser, EnterpriseUser
from checkcord.storage.postgres import tenant_context

logger = structlog.get_logger(__name__).bind(component="accounts")

EMAIL, PROFILE, MANUAL = "email", "profile", "manual"
PLATFORMS = ("slack", "ms_teams", "github")
_ALIASES = {"teams": "ms_teams", "msteams": "ms_teams"}
LOOKUP_LIMIT = 50
PROFILE_TTL = timedelta(days=1)
UNMATCHED_KEPT = 200

_cu = ChatUser.__table__.c
_eu = EnterpriseUser.__table__.c
_ac = AudienceContainer.__table__.c

_LINK_BY_EMAIL = """
UPDATE chat_users cu
SET internal_id = eu.id, link_source = :source, linked_at = now(), linked_by = NULL
FROM enterprise_users eu
WHERE cu.tenant_id = :tenant AND eu.tenant_id = :tenant
  AND cu.internal_id IS NULL
  AND cu.link_source IS DISTINCT FROM 'manual'
  AND cu.email IS NOT NULL
  AND lower(cu.email) = lower(eu.primary_email)
  AND eu.active
  AND (CAST(:platform AS text) IS NULL OR cu.platform = :platform)
RETURNING cu.platform, cu.native_id, eu.id
"""


@dataclass(frozen=True)
class Profile:
    native_id: str
    display_name: str | None = None
    email: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


Lookup = Callable[[list[str]], Awaitable[list[Profile]]]


class UnknownPerson(LookupError):
    pass


class UnknownPlatform(ValueError):
    pass


def platform_of(value: str) -> str:
    name = value.strip().lower()
    name = _ALIASES.get(name, name)
    if name not in PLATFORMS:
        raise UnknownPlatform(f"{value!r} is not a platform; use one of {', '.join(PLATFORMS)}")
    return name


def source_for(platform: str) -> str:
    """GitHub emails come from the public profile; chat emails from the directory."""
    return PROFILE if platform == "github" else EMAIL


def actor_label(actor: audit.Actor) -> str:
    return actor.user_email or actor.key_name or actor.channel


async def upsert(
    conn: AsyncConnection, tenant_id: UUID, platform: str, profiles: Iterable[Profile]
) -> None:
    """Create or update accounts. Never touches a link."""
    for p in profiles:
        stmt = pg_insert(ChatUser).values(
            tenant_id=tenant_id,
            platform=platform,
            native_id=p.native_id,
            display_name=p.display_name or p.native_id,
            email=p.email,
            metadata_=p.metadata,
        )
        updates: dict[str, Any] = {
            "metadata": ChatUser.metadata_.op("||")(stmt.excluded.metadata),
            "updated_at": func.now(),
        }
        if p.display_name:
            updates["display_name"] = stmt.excluded.display_name
        if p.email:
            updates["email"] = stmt.excluded.email
        await conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["tenant_id", "platform", "native_id"], set_=updates
            )
        )


async def link_by_email(
    conn: AsyncConnection, tenant_id: UUID, platform: str | None = None
) -> list[tuple[str, str, UUID]]:
    """Match unlinked accounts to active people by email, ignoring case. Returns the new links."""
    found: list[tuple[str, str, UUID]] = []
    for source, only in (
        ((source_for(platform), platform),)
        if platform
        else ((EMAIL, "slack"), (EMAIL, "ms_teams"), (PROFILE, "github"))
    ):
        rows = await conn.execute(
            text(_LINK_BY_EMAIL),
            {"tenant": str(tenant_id), "platform": only, "source": source},
        )
        found += [(r[0], r[1], r[2]) for r in rows]
    return found


def _stale(row: Any, now: datetime) -> bool:
    read = (row._mapping["metadata"] or {}).get("profile_read_at") if row is not None else None
    if not read:
        return True
    try:
        return datetime.fromisoformat(read) <= now - PROFILE_TTL
    except ValueError:
        return True


async def match(
    engine: AsyncEngine,
    tenant_id: UUID,
    platform: str,
    native_ids: Sequence[str],
    lookup: Lookup | None = None,
) -> tuple[dict[str, UUID], list[str]]:
    """Map native ids to people, and list those not matched.

    With ``lookup``, accounts not seen before, and unlinked ones not read in the last day,
    are read first, at most ``LOOKUP_LIMIT`` per call.
    """
    ids = list(dict.fromkeys(str(n) for n in native_ids))
    if not ids:
        return {}, []
    now = datetime.now(UTC)
    async with tenant_context(engine, tenant_id) as conn:
        known = {
            r.native_id: r
            for r in await conn.execute(
                select(_cu.native_id, _cu.internal_id, _cu.link_source, _cu.metadata).where(
                    (_cu.tenant_id == tenant_id)
                    & (_cu.platform == platform)
                    & _cu.native_id.in_(ids)
                )
            )
        }
    wanted = [
        n
        for n in ids
        if n not in known
        or (
            known[n].internal_id is None
            and known[n].link_source != MANUAL
            and _stale(known[n], now)
        )
    ][:LOOKUP_LIMIT]
    profiles: list[Profile] = []
    looked_up = False
    if lookup is not None and wanted:
        try:
            profiles = await lookup(wanted)
            looked_up = True
        except Exception as err:
            logger.warning("accounts.lookup_failed", platform=platform, error=str(err))
    async with tenant_context(engine, tenant_id) as conn:
        read = {p.native_id for p in profiles}
        stamp = {"profile_read_at": now.isoformat()}
        stamped = set(wanted) if looked_up else set()
        await upsert(
            conn,
            tenant_id,
            platform,
            [
                Profile(p.native_id, p.display_name, p.email, {**p.metadata, **stamp})
                for p in profiles
            ]
            + [
                Profile(n, metadata=stamp if n in stamped else {})
                for n in ids
                if n not in read and (n not in known or n in stamped)
            ],
        )
        await link_by_email(conn, tenant_id, platform)
        rows = await conn.execute(
            select(_cu.native_id, _cu.internal_id).where(
                (_cu.tenant_id == tenant_id)
                & (_cu.platform == platform)
                & _cu.native_id.in_(ids)
                & _cu.internal_id.is_not(None)
            )
        )
        matched = {r.native_id: r.internal_id for r in rows}
    return matched, [n for n in ids if n not in matched]


def _restricted_count() -> Any:
    return (
        select(func.count())
        .select_from(AudienceContainer.__table__)
        .where(
            (_ac.tenant_id == _cu.tenant_id)
            & (_ac.platform == _cu.platform)
            & ~_ac.is_public
            & _cu.native_id.op("=")(func.any(_ac.unmatched_accounts))
        )
        .scalar_subquery()
    )


def _account_row(r: Any) -> dict[str, Any]:
    return {
        "platform": r.platform,
        "account": r.native_id,
        "display_name": r.display_name,
        "source_email": r.email,
        "user_id": r.internal_id,
        "person_email": r.primary_email,
        "link_source": r.link_source,
        "linked_at": r.linked_at,
        "linked_by": r.linked_by,
        "unmatched_in": r.unmatched_in,
    }


async def list_accounts(
    engine: AsyncEngine,
    tenant_id: UUID,
    *,
    platform: str | None = None,
    matched: bool | None = None,
    limit: int = 100,
    cursor: tuple[str, str] | None = None,
) -> tuple[list[dict[str, Any]], tuple[str, str] | None]:
    stmt = (
        select(
            _cu.platform,
            _cu.native_id,
            _cu.display_name,
            _cu.email,
            _cu.internal_id,
            _cu.link_source,
            _cu.linked_at,
            _cu.linked_by,
            _eu.primary_email,
            _restricted_count().label("unmatched_in"),
        )
        .select_from(
            ChatUser.__table__.outerjoin(EnterpriseUser.__table__, _eu.id == _cu.internal_id)
        )
        .where(_cu.tenant_id == tenant_id)
    )
    if platform:
        stmt = stmt.where(_cu.platform == platform_of(platform))
    if matched is True:
        stmt = stmt.where(_cu.internal_id.is_not(None))
    elif matched is False:
        stmt = stmt.where(_cu.internal_id.is_(None))
    if cursor is not None:
        stmt = stmt.where(tuple_(_cu.platform, _cu.native_id) > tuple_(*cursor))
    stmt = stmt.order_by(_cu.platform, _cu.native_id).limit(limit + 1)
    async with tenant_context(engine, tenant_id) as conn:
        rows = [_account_row(r) for r in await conn.execute(stmt)]
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, ((rows[-1]["platform"], rows[-1]["account"]) if more else None)


async def unmatched_in_restricted(engine: AsyncEngine, tenant_id: UUID) -> int:
    """Distinct accounts that restricted audiences list as not matched to a person."""
    members = (
        select(_ac.platform, func.unnest(_ac.unmatched_accounts).label("account"))
        .where((_ac.tenant_id == tenant_id) & ~_ac.is_public)
        .subquery()
    )
    async with tenant_context(engine, tenant_id) as conn:
        return int(
            (
                await conn.execute(
                    select(func.count()).select_from(
                        select(members.c.platform, members.c.account).distinct().subquery()
                    )
                )
            ).scalar_one()
        )


async def _person(
    conn: AsyncConnection, tenant_id: UUID, email: str | None, user_id: UUID | None
) -> Any:
    where = (
        _eu.id == user_id
        if user_id is not None
        else func.lower(_eu.primary_email) == (email or "").strip().lower()
    )
    rows = (
        await conn.execute(
            select(_eu.id, _eu.primary_email).where((_eu.tenant_id == tenant_id) & where)
        )
    ).all()
    if len(rows) != 1:
        raise UnknownPerson(f"no person {email or user_id}")
    return rows[0]


@dataclass
class Linked:
    platform: str
    account: str
    user_id: UUID | None
    person_email: str | None
    created: bool = False


async def _link(
    conn: AsyncConnection,
    tenant_id: UUID,
    platform: str,
    account: str,
    person: Any,
    actor: audit.Actor,
) -> Linked:
    account = account.strip()
    created = (
        await conn.execute(
            pg_insert(ChatUser)
            .values(
                tenant_id=tenant_id,
                platform=platform,
                native_id=account,
                display_name=account,
                metadata_={},
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "platform", "native_id"])
            .returning(_cu.id)
        )
    ).first() is not None
    await conn.execute(
        update(ChatUser)
        .where(
            (_cu.tenant_id == tenant_id) & (_cu.platform == platform) & (_cu.native_id == account)
        )
        .values(
            internal_id=person.id if person is not None else None,
            link_source=MANUAL,
            linked_at=func.now(),
            linked_by=actor_label(actor),
            updated_at=func.now(),
        )
    )
    return Linked(
        platform,
        account,
        person.id if person is not None else None,
        person.primary_email if person is not None else None,
        created,
    )


async def link(
    engine: AsyncEngine,
    tenant_id: UUID,
    platform: str,
    account: str,
    *,
    actor: audit.Actor,
    email: str | None = None,
    user_id: UUID | None = None,
    redis: Redis | None = None,
) -> Linked:
    """Match an account to a person by hand; a sync never changes it. Creates the account
    when it was not seen yet."""
    platform = platform_of(platform)
    async with tenant_context(engine, tenant_id) as conn:
        person = await _person(conn, tenant_id, email, user_id)
        done = await _link(conn, tenant_id, platform, account, person, actor)
        await _audit_link(conn, tenant_id, actor, done)
    await refresh_affected(engine, tenant_id, [done], trigger="account_linked", redis=redis)
    return done


async def unlink(
    engine: AsyncEngine,
    tenant_id: UUID,
    platform: str,
    account: str,
    *,
    actor: audit.Actor,
    redis: Redis | None = None,
) -> Linked | None:
    """Take an account's person away; a sync does not match it again. None when unknown."""
    platform = platform_of(platform)
    account = account.strip()
    this = (_cu.tenant_id == tenant_id) & (_cu.platform == platform) & (_cu.native_id == account)
    async with tenant_context(engine, tenant_id) as conn:
        row = (
            await conn.execute(
                select(_cu.internal_id, _eu.primary_email)
                .select_from(
                    ChatUser.__table__.outerjoin(
                        EnterpriseUser.__table__, _eu.id == _cu.internal_id
                    )
                )
                .where(this)
                .with_for_update(of=ChatUser.__table__)
            )
        ).first()
        if row is None:
            return None
        await _link(conn, tenant_id, platform, account, None, actor)
        await audit.record(
            conn,
            tenant_id,
            actor,
            "account.unlinked",
            params={
                "platform": platform,
                "account": account,
                "person_email": row.primary_email,
                "link_source": MANUAL,
            },
            record_ids=[row.internal_id] if row.internal_id else None,
        )
    gone = Linked(platform, account, row.internal_id, row.primary_email)
    await refresh_affected(engine, tenant_id, [gone], trigger="account_unlinked", redis=redis)
    return gone


async def _audit_link(
    conn: AsyncConnection, tenant_id: UUID, actor: audit.Actor, done: Linked
) -> None:
    await audit.record(
        conn,
        tenant_id,
        actor,
        "account.linked",
        params={
            "platform": done.platform,
            "account": done.account,
            "person_email": done.person_email,
            "link_source": MANUAL,
        },
        record_ids=[done.user_id] if done.user_id else None,
    )


@dataclass
class Imported:
    linked: int = 0
    unknown_accounts: int = 0
    unknown_people: int = 0
    rejected: list[str] = field(default_factory=list)


async def import_links(
    engine: AsyncEngine,
    tenant_id: UUID,
    rows: Iterable[dict[str, str]],
    *,
    actor: audit.Actor,
    redis: Redis | None = None,
) -> Imported:
    """Link accounts from ``platform,account,email`` rows. An account not seen yet is created."""
    result = Imported()
    done: list[Linked] = []
    async with tenant_context(engine, tenant_id) as conn:
        for n, row in enumerate(rows, start=2):
            try:
                platform = platform_of(row.get("platform") or "")
                account = (row.get("account") or "").strip()
                if not account:
                    raise ValueError("no account")
            except ValueError as err:
                result.rejected.append(f"line {n}: {err}")
                continue
            try:
                person = await _person(conn, tenant_id, row.get("email"), None)
            except UnknownPerson:
                result.unknown_people += 1
                continue
            linked = await _link(conn, tenant_id, platform, account, person, actor)
            await _audit_link(conn, tenant_id, actor, linked)
            result.linked += 1
            result.unknown_accounts += linked.created
            done.append(linked)
    await refresh_affected(engine, tenant_id, done, trigger="account_linked", redis=redis)
    return result


def read_csv(lines: Iterable[str]) -> list[dict[str, str]]:
    reader = csv.DictReader(lines)
    header = [h.strip().lower() for h in reader.fieldnames or []]
    if header[:3] != ["platform", "account", "email"]:
        raise ValueError("the header must be platform,account,email")
    reader.fieldnames = header
    return [{k: (v or "").strip() for k, v in r.items() if k} for r in reader]


async def refresh_affected(
    engine: AsyncEngine,
    tenant_id: UUID,
    changed: Sequence[Linked | tuple[str, str, UUID]],
    *,
    trigger: str,
    redis: Redis | None = None,
) -> int:
    """Read again every stored audience that lists a changed account as not matched, or one
    of its people as a member. Returns how many were refreshed."""
    from checkcord.services import audiences

    pairs: set[tuple[str, str]] = set()
    people: set[UUID] = set()
    for c in changed:
        platform, account, person = (
            (c.platform, c.account, c.user_id) if isinstance(c, Linked) else c
        )
        pairs.add((platform, account))
        if person is not None:
            people.add(person)
    if not pairs:
        return 0
    conditions = [
        and_(_ac.platform == p, literal(a).op("=")(func.any(_ac.unmatched_accounts)))
        for p, a in sorted(pairs)
    ]
    platforms = {p for p, _ in pairs}
    conditions += [
        and_(_ac.platform.in_(platforms), literal(u).op("=")(func.any(_ac.member_ids)))
        for u in sorted(people, key=str)
    ]
    async with tenant_context(engine, tenant_id) as conn:
        found = (
            await conn.execute(
                select(_ac.platform, _ac.container)
                .where((_ac.tenant_id == tenant_id) & ~_ac.is_public & or_(*conditions))
                .order_by(_ac.platform, _ac.container)
            )
        ).all()
    for platform, container in found:
        try:
            await audiences.refresh_stored(
                engine, tenant_id, platform, container, trigger=trigger, redis=redis
            )
        except Exception:
            logger.exception("accounts.refresh_failed", platform=platform, container=container)
    return len(found)


async def after_sync(
    engine: AsyncEngine, tenant_id: UUID, platform: str, redis: Redis | None = None
) -> int:
    """Match what a directory sync brought in and refresh the audiences it changes."""
    try:
        async with tenant_context(engine, tenant_id) as conn:
            linked = await link_by_email(conn, tenant_id, platform)
        if linked:
            await refresh_affected(engine, tenant_id, linked, trigger="directory_sync", redis=redis)
        return len(linked)
    except Exception:
        logger.exception("accounts.after_sync_failed", platform=platform)
        return 0
