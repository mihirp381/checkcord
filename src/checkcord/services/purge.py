"""Delete events and everything made from them: links, records, meeting extractions, chat state.

A record is deleted when no linked event is left. A record whose base event is deleted takes
the earliest remaining event as its base, with that event's text and audience.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy import (
    column,
    delete,
    exists,
    false,
    func,
    select,
    table,
    text,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.core.config import get_settings
from checkcord.services.audiences import rewrite_files
from checkcord.services.connections import connection_key
from checkcord.storage import audit, redis_trees
from checkcord.storage.models import (
    AgentLedger,
    APIKey,
    ASTNode,
    AudienceContainer,
    AuditLogEntry,
    CanonicalContext,
    ChatUser,
    CIFailureContextLink,
    ContextEventLink,
    EnterpriseUser,
    RemovedSource,
    UserRepoAffinity,
    WorkflowDefinitionRecord,
)
from checkcord.storage.partitions import drop_empty_partitions
from checkcord.storage.postgres import (
    PostgresEventStore,
    events_table,
    list_tenant_ids,
    source_id_hash,
    tenant_context,
)

logger = structlog.get_logger(__name__).bind(component="purge")

BATCH_SIZE = 500

_ev = events_table.c
_links = ContextEventLink.__table__
_records = CanonicalContext.__table__
_containers = AudienceContainer.__table__
_segments = table(
    "meeting_discourse_segments",
    column("segment_id"),
    column("parent_event_id"),
    column("tenant_id"),
)
_EXTRACTIONS = {
    kind: table(name, column("id"), column("segment_id"))
    for kind, name in (
        ("adr", "extracted_adrs"),
        ("constraint", "extracted_constraints"),
        ("risk", "extracted_risks"),
    )
}
_REPO_TABLES = (ASTNode.__table__, WorkflowDefinitionRecord.__table__)
_CONTAINER_DETAIL = {"slack": ("slack", "team_id"), "teams": ("ms_teams", "ms_tenant_id")}
_removed = RemovedSource.__table__
REMEMBERED = frozenset({"event", "source", "record", "review", "person"})


@dataclass(frozen=True)
class Selection:
    kind: str
    params: dict[str, Any]
    where: Any
    remove_source: bool = False
    containers: Any = None
    repo: str | None = None
    connection: str | None = None
    authors: frozenset[str] = frozenset()
    records: tuple[UUID, ...] = ()


def events(ids: list[UUID]) -> Selection:
    return Selection("event", {"event_ids": [str(i) for i in ids]}, _ev.event_id.in_(ids))


def source(platform: str, container: str) -> Selection:
    return Selection(
        "source",
        {"platform": platform, "container": container},
        (_ev.source_platform == platform) & (_ev.audience_container == container),
        remove_source=True,
        containers=(_containers.c.platform == platform) & (_containers.c.container == container),
        repo=container if platform == "github" else None,
    )


def connection(provider: str, identifier: str) -> Selection:
    key = connection_key(provider, identifier)
    identifier = identifier.strip()
    where = _ev.connection == key
    containers = None
    if provider == "github":
        where = where | ((_ev.source_platform == "github") & (_ev.audience_container == identifier))
        containers = (_containers.c.platform == "github") & (_containers.c.container == identifier)
    elif provider in _CONTAINER_DETAIL:
        platform, detail = _CONTAINER_DETAIL[provider]
        containers = (_containers.c.platform == platform) & (
            _containers.c.details[detail].astext == identifier
        )
    return Selection(
        "connection",
        {"provider": provider, "identifier": identifier},
        where,
        remove_source=True,
        containers=containers,
        repo=identifier if provider == "github" else None,
        connection=key,
    )


def record(record_id: UUID) -> Selection:
    """The record and the events linked to it and to no other record."""
    other = _links.alias("other")
    only_here = (
        select(_links.c.event_id)
        .where(_links.c.canonical_id == record_id)
        .where(
            ~exists().where(
                (other.c.event_id == _links.c.event_id) & (other.c.canonical_id != record_id)
            )
        )
    )
    return Selection(
        "record", {"record_id": str(record_id)}, _ev.event_id.in_(only_here), records=(record_id,)
    )


def held(key: str) -> Selection:
    """The events held for review under one connection."""
    return Selection("review", {"connection": key}, _ev.held_connection == key)


def older_than(cutoff: datetime) -> Selection:
    return Selection("retention", {"before": cutoff.isoformat()}, _ev.happened_at < cutoff)


def authored_by(user_id: UUID, email: str, accounts: list[tuple[str, str]]) -> Selection:
    """Events by the person, by any account linked to them, or under their email."""
    author = _ev.author
    where = (author["internal_id"].astext == str(user_id)) | (
        func.lower(author["email"].astext) == email.lower()
    )
    if accounts:
        where = where | tuple_(_ev.source_platform, author["source_id"].astext).in_(
            sorted(accounts)
        )
    return Selection(
        "person",
        {"user_id": str(user_id)},
        where,
        authors=frozenset(native for _, native in accounts),
    )


def remembered_as(selection: Selection, remember: bool = True) -> str | None:
    return selection.kind if remember and selection.kind in REMEMBERED else None


@dataclass
class Purged:
    events: int = 0
    segments: int = 0
    code_symbols: int = 0
    containers: int = 0
    chat_state: int = 0
    dry_run: bool = False
    deleted: set[UUID] = field(default_factory=set)
    updated: set[UUID] = field(default_factory=set)
    chat_messages: set[tuple[str, str, str]] = field(default_factory=set)
    chat_outcomes: set[tuple[str, str, tuple[str, ...]]] = field(default_factory=set)

    @property
    def records_deleted(self) -> int:
        return len(self.deleted)

    @property
    def records_updated(self) -> int:
        return len(self.updated - self.deleted)

    def counts(self) -> dict[str, int]:
        return {
            "events": self.events,
            "records_deleted": self.records_deleted,
            "records_updated": self.records_updated,
            "meeting_segments": self.segments,
            "code_symbols": self.code_symbols,
            "audience_containers": self.containers,
            "chat_buffers_and_trees": self.chat_state,
        }


def _selected(tenant_id: UUID, selection: Selection) -> Any:
    return (_ev.tenant_id == tenant_id) & selection.where


async def count(engine: AsyncEngine, tenant_id: UUID, selection: Selection) -> Purged:
    """What ``purge`` would delete, without deleting anything."""
    ids = select(_ev.event_id).where(_selected(tenant_id, selection))
    linked = select(_links.c.canonical_id).where(
        (_links.c.tenant_id == tenant_id) & _links.c.event_id.in_(ids)
    )
    kept_link = exists().where(
        (_links.c.canonical_id == _records.c.id) & _links.c.event_id.not_in(ids)
    )
    touched = (_records.c.tenant_id == tenant_id) & (
        _records.c.id.in_(linked) | _records.c.id.in_(selection.records)
    )
    doomed = ~kept_link | _records.c.id.in_(selection.records)
    async with tenant_context(engine, tenant_id, include_held=True, include_retired=True) as conn:
        result = Purged(dry_run=True)
        result.events = (
            await conn.execute(select(func.count()).select_from(ids.subquery()))
        ).scalar_one()
        found = await conn.execute(select(_records.c.id).where(touched & doomed))
        result.deleted = set(found.scalars())
        found = await conn.execute(select(_records.c.id).where(touched))
        result.updated = set(found.scalars())
        result.segments = (
            await conn.execute(
                select(func.count())
                .select_from(_segments)
                .where((_segments.c.tenant_id == tenant_id) & _segments.c.parent_event_id.in_(ids))
            )
        ).scalar_one()
        if selection.repo:
            result.code_symbols = (
                await conn.execute(
                    select(func.count())
                    .select_from(ASTNode.__table__)
                    .where(
                        (ASTNode.tenant_id == tenant_id)
                        & (ASTNode.repo_identifier == selection.repo)
                    )
                )
            ).scalar_one()
    return result


async def _batch(
    engine: AsyncEngine,
    tenant_id: UUID,
    where: Any,
    limit: int,
    result: Purged,
    pairs: set[tuple[str, str]],
    remember: str | None = None,
) -> tuple[int, list[str]]:
    async with tenant_context(engine, tenant_id, include_held=True, include_retired=True) as conn:
        meta = _ev["metadata"]
        rows = (
            await conn.execute(
                select(
                    _ev.event_id,
                    _ev.source_platform,
                    _ev.audience_container,
                    _ev.source_id,
                    meta["chat_message"]["message_id"].astext.label("message_id"),
                    meta["chat_source"]["channel_id"].astext.label("outcome_channel"),
                    meta["chat_source"]["discussion_message_ids"].label("discussion"),
                )
                .where(where)
                .order_by(_ev.happened_at, _ev.event_id)
                .limit(limit)
            )
        ).all()
        if not rows:
            return 0, []
        ids = [r.event_id for r in rows]
        pairs.update(
            (r.source_platform, r.audience_container) for r in rows if r.audience_container
        )
        for r in rows:
            if r.message_id and r.audience_container:
                result.chat_messages.add((r.source_platform, r.audience_container, r.message_id))
            if r.outcome_channel and r.discussion:
                result.chat_outcomes.add(
                    (r.source_platform, r.outcome_channel, tuple(sorted(r.discussion)))
                )

        linked = set(
            (
                await conn.execute(
                    select(_links.c.canonical_id)
                    .where((_links.c.tenant_id == tenant_id) & _links.c.event_id.in_(ids))
                    .distinct()
                )
            ).scalars()
        )
        segment_ids = list(
            (
                await conn.execute(
                    select(_segments.c.segment_id).where(
                        (_segments.c.tenant_id == tenant_id) & _segments.c.parent_event_id.in_(ids)
                    )
                )
            ).scalars()
        )
        derived: list[str] = []
        if segment_ids:
            for kind, extractions in _EXTRACTIONS.items():
                found = await conn.execute(
                    select(extractions.c.id).where(extractions.c.segment_id.in_(segment_ids))
                )
                derived.extend(f"extraction-{kind}-{i}" for i in found.scalars())
            await conn.execute(delete(_segments).where(_segments.c.segment_id.in_(segment_ids)))
            result.segments += len(segment_ids)

        await conn.execute(
            delete(CIFailureContextLink).where(
                (CIFailureContextLink.tenant_id == tenant_id)
                & CIFailureContextLink.ci_event_id.in_(ids)
            )
        )
        await conn.execute(
            update(AgentLedger)
            .where((AgentLedger.tenant_id == tenant_id) & AgentLedger.event_id.in_(ids))
            .values(event_id=None)
        )
        await conn.execute(
            delete(_links).where((_links.c.tenant_id == tenant_id) & _links.c.event_id.in_(ids))
        )
        await conn.execute(
            delete(events_table).where((_ev.tenant_id == tenant_id) & _ev.event_id.in_(ids))
        )
        if remember:
            removed = {(r.source_platform, source_id_hash(r.source_id)) for r in rows}
            await conn.execute(
                pg_insert(_removed)
                .values(
                    [
                        {
                            "tenant_id": tenant_id,
                            "source_platform": platform,
                            "source_id_hash": digest,
                            "reason": remember,
                        }
                        for platform, digest in sorted(removed)
                    ]
                )
                .on_conflict_do_nothing()
            )
        result.events += len(ids)

        if linked:
            gone = set(
                (
                    await conn.execute(
                        delete(_records)
                        .where(
                            (_records.c.tenant_id == tenant_id)
                            & _records.c.id.in_(linked)
                            & ~exists().where(_links.c.canonical_id == _records.c.id)
                        )
                        .returning(_records.c.id)
                    )
                ).scalars()
            )
            store = PostgresEventStore(conn)
            for record_id in sorted(linked - gone, key=str):
                await promote(conn, tenant_id, record_id)
                await store.recompute_context_access_control(tenant_id, record_id)
            result.deleted |= gone
            result.updated |= linked - gone
    return len(ids), derived


async def promote(conn: Any, tenant_id: UUID, record_id: UUID) -> bool:
    """When the record's base event is gone, make the earliest remaining event with a stored
    excerpt (else the earliest) its base: its text becomes the record's text."""
    base = (
        await conn.execute(
            select(_records.c.base_event_id).where(
                (_records.c.tenant_id == tenant_id) & (_records.c.id == record_id)
            )
        )
    ).scalar_one_or_none()
    if base is not None:
        still = await conn.execute(
            select(_links.c.event_id).where(
                (_links.c.canonical_id == record_id) & (_links.c.event_id == base)
            )
        )
        if still.first() is not None:
            return False
    found = (
        await conn.execute(
            select(
                _links.c.event_id,
                _links.c.excerpt_redacted,
                _links.c.excerpt_version,
                _links.c.excerpt_pending,
                _ev.content_clean,
            )
            .select_from(_links.join(events_table, _links.c.event_id == _ev.event_id))
            .where(
                (_links.c.tenant_id == tenant_id)
                & (_links.c.canonical_id == record_id)
                & (_ev.tenant_id == tenant_id)
            )
            .order_by(_links.c.excerpt_redacted.is_(None), _links.c.created_at, _links.c.event_id)
            .limit(1)
        )
    ).first()
    if found is None:
        return False
    summary = found.content_clean or found.excerpt_redacted or ""
    await conn.execute(
        update(_records)
        .where((_records.c.tenant_id == tenant_id) & (_records.c.id == record_id))
        .values(
            summary=summary,
            summary_redacted=found.excerpt_redacted,
            redaction_version=found.excerpt_version,
            redaction_pending=bool(found.excerpt_pending),
            content_hash=hashlib.sha256(summary.encode("utf-8")).hexdigest(),
            base_event_id=found.event_id,
            updated_at=func.now(),
        )
    )
    await conn.execute(
        update(_links)
        .where((_links.c.canonical_id == record_id) & (_links.c.event_id == found.event_id))
        .values(excerpt_redacted=None, excerpt_version=None, excerpt_pending=False)
    )
    return True


async def purge(
    engine: AsyncEngine,
    tenant_id: UUID,
    selection: Selection,
    *,
    actor: audit.Actor,
    redis: Redis | None = None,
    batch_size: int = BATCH_SIZE,
    audit_empty: bool = True,
    rebuild: bool = True,
    remember: bool = True,
) -> Purged:
    """Delete the selected events of one tenant and what was made from them, in batches.

    Removing chat messages by event, source or person rebuilds the discussions they were in.
    """
    reason = remembered_as(selection, remember)
    result = Purged()
    pairs: set[tuple[str, str]] = set()
    pending = [_selected(tenant_id, selection)]
    while pending:
        where = pending.pop()
        while True:
            done, derived = await _batch(
                engine, tenant_id, where, batch_size, result, pairs, reason
            )
            if derived:
                pending.append((_ev.tenant_id == tenant_id) & _ev.source_id.in_(derived))
            if done < batch_size:
                break

    async with tenant_context(engine, tenant_id, include_held=True, include_retired=True) as conn:
        if selection.records:
            gone = await conn.execute(
                delete(_records)
                .where((_records.c.tenant_id == tenant_id) & _records.c.id.in_(selection.records))
                .returning(_records.c.id)
            )
            result.deleted |= set(gone.scalars())
        if selection.repo:
            for repo_table in _REPO_TABLES:
                removed = await conn.execute(
                    delete(repo_table).where(
                        (repo_table.c.tenant_id == tenant_id)
                        & (repo_table.c.repo_identifier == selection.repo)
                    )
                )
                if repo_table is ASTNode.__table__:
                    result.code_symbols = removed.rowcount
        if selection.remove_source:
            mine = _containers.c.tenant_id == tenant_id
            which = selection.containers if selection.containers is not None else false()
            if pairs:
                pair = tuple_(_containers.c.platform, _containers.c.container)
                which = which | pair.in_(sorted(pairs))
            no_events = ~exists().where(
                (_ev.tenant_id == tenant_id)
                & (_ev.source_platform == _containers.c.platform)
                & (_ev.audience_container == _containers.c.container)
            )
            removed = await conn.execute(delete(_containers).where(mine & which & no_events))
            result.containers = removed.rowcount
        if result.events or audit_empty:
            await audit.record(
                conn,
                tenant_id,
                actor,
                "data.purged",
                params={"selection": selection.kind, **selection.params, **result.counts()},
                result_count=result.events,
            )

    if redis is not None:
        chat = ("slack", "ms_teams")
        channels = {p for p in pairs if p[0] in chat} if selection.remove_source else set()
        if channels or selection.connection or selection.authors:
            result.chat_state = await redis_trees.drop_chat_state(
                redis,
                tenant_id,
                channels=channels,
                connection=selection.connection,
                authors=selection.authors,
            )
        by_channel: dict[tuple[str, str], list[str]] = {}
        for platform, channel_id, message_id in result.chat_messages:
            by_channel.setdefault((platform, channel_id), []).append(message_id)
        for (platform, channel_id), message_ids in by_channel.items():
            await redis_trees.remove_messages(redis, tenant_id, platform, channel_id, message_ids)
    if result.deleted or result.updated:
        touched = sorted(result.deleted | result.updated, key=str)
        await rewrite_files(redis, tenant_id, "purge", selection.kind, touched)
    if rebuild and result.chat_messages and selection.kind in ("event", "source", "person"):
        from checkcord.services import chat_messages

        await chat_messages.rebuild_discussions(
            engine,
            redis,
            tenant_id,
            result.chat_messages,
            f"purge.{selection.kind}",
            actor=actor,
            removed=result.chat_outcomes,
        )
    logger.info("purged", tenant_id=str(tenant_id), selection=selection.kind, **result.counts())
    return result


async def purge_expired(
    engine: AsyncEngine, redis: Redis | None = None, now: datetime | None = None
) -> int:
    """Scheduled job: delete events older than ``EVENT_RETENTION_DAYS``. Returns how many."""
    days = get_settings().event_retention_days
    if days is None:
        return 0
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    tenants = await list_tenant_ids(engine)
    total = 0
    for tenant_id in tenants:
        try:
            purged = await purge(
                engine,
                tenant_id,
                older_than(cutoff),
                actor=audit.Actor(channel=audit.API),
                redis=redis,
                audit_empty=False,
            )
            total += purged.events
        except Exception:
            logger.exception("purge_expired_failed", tenant_id=str(tenant_id))
    try:
        await drop_empty_partitions(engine, cutoff.date())
    except Exception:
        logger.exception("drop_empty_partitions_failed")
    return total


async def removed_counts(engine: AsyncEngine, tenant_id: UUID) -> dict[str, int]:
    async with tenant_context(engine, tenant_id) as conn:
        rows = await conn.execute(
            select(_removed.c.source_platform, func.count())
            .where(_removed.c.tenant_id == tenant_id)
            .group_by(_removed.c.source_platform)
            .order_by(_removed.c.source_platform)
        )
        return {platform: n for platform, n in rows}


async def clear_removed(
    engine: AsyncEngine, tenant_id: UUID, *, actor: audit.Actor, platform: str | None = None
) -> int:
    where = _removed.c.tenant_id == tenant_id
    if platform:
        where = where & (_removed.c.source_platform == platform)
    async with tenant_context(engine, tenant_id) as conn:
        cleared = (await conn.execute(delete(_removed).where(where))).rowcount or 0
        await audit.record(
            conn,
            tenant_id,
            actor,
            "removed.cleared",
            params={"platform": platform, "count": cleared},
            result_count=cleared,
        )
    return cleared


@dataclass(frozen=True)
class PersonDeleted:
    user_id: UUID
    keys: int
    purged: Purged


_STRIP = """jsonb_set(jsonb_set({col}, '{{admin_overrides}}',
    COALESCE({col}->'admin_overrides', '[]'::jsonb) - :uid_text),
    '{{allowed_identity_ids}}',
    COALESCE({col}->'allowed_identity_ids', '[]'::jsonb) - :uid_text)"""
_NAMES = """(jsonb_exists(COALESCE({col}->'admin_overrides', '[]'::jsonb), :uid_text)
    OR jsonb_exists(COALESCE({col}->'allowed_identity_ids', '[]'::jsonb), :uid_text))"""
_UID = "CAST(:uid AS uuid)"
_TENANT = "tenant_id = CAST(:tenant AS uuid)"
_UNNAME = (
    text(
        f"UPDATE events SET access_allowed_ids = array_remove(access_allowed_ids, {_UID}), "
        f"access_control = {_STRIP.format(col='access_control')} "
        f"WHERE {_TENANT} AND ({_UID} = ANY(access_allowed_ids) "
        f"OR {_NAMES.format(col='access_control')})"
    ),
    text(
        f"UPDATE canonical_contexts SET access_control = {_STRIP.format(col='access_control')} "
        f"WHERE {_TENANT} AND {_NAMES.format(col='access_control')}"
    ),
    text(
        f"UPDATE audience_containers SET member_ids = array_remove(member_ids, {_UID}) "
        f"WHERE {_TENANT} AND {_UID} = ANY(member_ids)"
    ),
)


async def find_person(
    engine: AsyncEngine, tenant_id: UUID, *, user_id: UUID | None = None, email: str = ""
) -> tuple[UUID, Selection] | None:
    """The person and the selection of the events they wrote."""
    if user_id is not None:
        match = EnterpriseUser.id == user_id
    else:
        match = func.lower(EnterpriseUser.primary_email) == email.strip().lower()
    async with tenant_context(engine, tenant_id, include_held=True, include_retired=True) as conn:
        person = (
            await conn.execute(
                select(EnterpriseUser.id, EnterpriseUser.primary_email).where(
                    (EnterpriseUser.tenant_id == tenant_id) & match
                )
            )
        ).one_or_none()
        if person is None:
            return None
        accounts = [
            (r.platform, r.native_id)
            for r in await conn.execute(
                select(ChatUser.platform, ChatUser.native_id).where(
                    (ChatUser.tenant_id == tenant_id) & (ChatUser.internal_id == person.id)
                )
            )
        ]
    return person.id, authored_by(person.id, person.primary_email, accounts)


async def delete_person(
    engine: AsyncEngine,
    tenant_id: UUID,
    *,
    actor: audit.Actor,
    user_id: UUID | None = None,
    email: str = "",
    redis: Redis | None = None,
    remember: bool = True,
) -> PersonDeleted | None:
    """Delete a person: their keys, the events they wrote, their place in every audience."""
    found = await find_person(engine, tenant_id, user_id=user_id, email=email)
    if found is None:
        return None
    person_id, selection = found
    purged = await purge(engine, tenant_id, selection, actor=actor, redis=redis, remember=remember)

    values = {"uid": str(person_id), "uid_text": str(person_id), "tenant": str(tenant_id)}
    async with tenant_context(engine, tenant_id, include_held=True, include_retired=True) as conn:
        for statement in _UNNAME:
            await conn.execute(statement, values)
        accounts = (
            await conn.execute(
                delete(ChatUser).where(
                    (ChatUser.tenant_id == tenant_id) & (ChatUser.internal_id == person_id)
                )
            )
        ).rowcount
        await conn.execute(
            delete(UserRepoAffinity).where(
                (UserRepoAffinity.tenant_id == tenant_id) & (UserRepoAffinity.user_id == person_id)
            )
        )
        mine = (APIKey.tenant_id == tenant_id) & (APIKey.user_id == person_id)
        keys = (await conn.execute(delete(APIKey).where(mine))).rowcount
        await conn.execute(
            delete(EnterpriseUser).where(
                (EnterpriseUser.tenant_id == tenant_id) & (EnterpriseUser.id == person_id)
            )
        )
        await audit.record(
            conn,
            tenant_id,
            actor,
            "user.deleted",
            params={
                "keys": keys,
                "accounts": accounts,
                "events": purged.events,
                "records_deleted": purged.records_deleted,
                "records_updated": purged.records_updated,
            },
            record_ids=[person_id],
        )
    from checkcord.services import connection_audiences

    await connection_audiences.resolve_again(engine, tenant_id, redis)
    return PersonDeleted(person_id, keys, purged)


async def prune_audit(
    owner_engine: AsyncEngine, days: int, *, dry_run: bool = False, now: datetime | None = None
) -> int:
    """Delete audit entries older than ``days`` on the schema-owner connection."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    entries = AuditLogEntry.__table__
    old = entries.c.created_at < cutoff
    async with owner_engine.begin() as conn:
        if dry_run:
            query = select(func.count()).select_from(entries).where(old)
            return int((await conn.execute(query)).scalar_one())
        deleted = await conn.execute(delete(entries).where(old).returning(entries.c.tenant_id))
        per_tenant: dict[UUID, int] = {}
        for tenant_id in deleted.scalars():
            per_tenant[tenant_id] = per_tenant.get(tenant_id, 0) + 1
        for tenant_id, n in per_tenant.items():
            await audit.record(
                conn,
                tenant_id,
                audit.CLI_ACTOR,
                "audit.pruned",
                params={"before": cutoff.isoformat()},
                result_count=n,
            )
    return sum(per_tenant.values())
