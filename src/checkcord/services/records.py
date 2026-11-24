"""Admin views and changes of one record outside the review flow."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from checkcord.core import redaction
from checkcord.models.enums import Severity
from checkcord.services import record_notes
from checkcord.storage import audit
from checkcord.storage.models import (
    CanonicalContext,
    ContextEventLink,
    ContextRepoScope,
    RecordReplacementSuggestion,
)
from checkcord.storage.postgres import decisions_or_blocking, events_table, tenant_context

SeverityName = Literal["blocking", "advisory"]
SEVERITIES: dict[str, str] = {
    "blocking": Severity.HARD_BLOCK.value,
    "advisory": Severity.SOFT_WARN.value,
}

_records = CanonicalContext.__table__
_links = ContextEventLink.__table__
_scopes = ContextRepoScope.__table__
_suggestions = RecordReplacementSuggestion.__table__
_ev = events_table.c

State = Literal["live", "held", "retired"]

_STATES_REPLACEMENT = re.compile(
    r"\b(?:supersed(?:e|es|ed|ing)"
    r"|(?:replac|revers|overrid)(?:e|es|ed|ing)\s+(?:the\s+|our\s+)?"
    r"(?:earlier|previous|old|prior|existing|former|original)"
    r"|no\s+longer\s+(?:applies|holds|stands))\b",
    re.IGNORECASE,
)


def severity_name(value: str) -> SeverityName:
    return "blocking" if value == Severity.HARD_BLOCK.value else "advisory"


def states_replacement(text: str) -> bool:
    """The text says it replaces an earlier decision."""
    return bool(_STATES_REPLACEMENT.search(text or ""))


UPDATES_SHOWN = 20
DECISION_TYPES = frozenset({"adr", "constraint", "business_requirement"})


def replaceable_by(older: str | None, newer: str | None) -> bool:
    """A record of type ``newer`` may replace one of type ``older``."""
    older, newer = older or "general", newer or "general"
    return older == newer or (older in DECISION_TYPES and newer in DECISION_TYPES)


def _who(actor: audit.Actor) -> str:
    if actor.user_email:
        return actor.user_email
    return f"key:{actor.key_name}" if actor.key_name else actor.channel


def _state(row: Any) -> State:
    if row["retired_at"] is not None:
        return "retired"
    return "held" if row["held_connection"] else "live"


async def set_severity(
    engine: AsyncEngine,
    tenant_id: UUID,
    record_id: UUID,
    severity: SeverityName,
    actor: audit.Actor,
) -> bool:
    """False when the record is not found, is held for review, or is retired."""
    this = (_records.c.tenant_id == tenant_id) & (_records.c.id == record_id)
    async with tenant_context(engine, tenant_id) as conn:
        before = await conn.scalar(select(_records.c.severity).where(this).with_for_update())
        if before is None:
            return False
        await conn.execute(
            update(_records)
            .where(this)
            .values(severity=SEVERITIES[severity], updated_at=func.now())
        )
        await audit.record(
            conn,
            tenant_id,
            actor,
            "context.severity_changed",
            params={"from": severity_name(before), "to": severity},
            record_ids=[record_id],
        )
    return True


TypeName = Literal["adr", "constraint", "business_requirement", "risk", "general"]
TYPES: tuple[TypeName, ...] = ("adr", "constraint", "business_requirement", "risk", "general")


async def set_type(
    engine: AsyncEngine,
    tenant_id: UUID,
    record_id: UUID,
    context_type: TypeName,
    actor: audit.Actor,
) -> bool:
    """False when the record is not found, is held for review, or is retired."""
    this = (_records.c.tenant_id == tenant_id) & (_records.c.id == record_id)
    async with tenant_context(engine, tenant_id) as conn:
        row = (
            await conn.execute(select(_records.c.context_type).where(this).with_for_update())
        ).first()
        if row is None:
            return False
        before = row.context_type or "general"
        await conn.execute(
            update(_records).where(this).values(context_type=context_type, updated_at=func.now())
        )
        await audit.record(
            conn,
            tenant_id,
            actor,
            "record.type_changed",
            params={"from": before, "to": context_type},
            record_ids=[record_id],
        )
    return True


async def describe(
    engine: AsyncEngine, tenant_id: UUID, record_id: UUID, actor: audit.Actor
) -> dict[str, Any] | None:
    """A record with its redacted summary, severity, scope, audience size and sources."""
    this = (_records.c.tenant_id == tenant_id) & (_records.c.id == record_id)
    async with tenant_context(engine, tenant_id, include_retired=True) as conn:
        row = (await conn.execute(select(_records).where(this))).mappings().first()
        if row is None:
            return None
        linked = _links.join(
            events_table,
            (_ev.tenant_id == _links.c.tenant_id) & (_ev.event_id == _links.c.event_id),
        )
        mine = (_links.c.tenant_id == tenant_id) & (_links.c.canonical_id == record_id)
        count, first, last = (
            await conn.execute(
                select(func.count(), func.min(_ev.happened_at), func.max(_ev.happened_at))
                .select_from(linked)
                .where(mine)
            )
        ).one()
        suggested = await conn.execute(
            select(_suggestions.c.id, _suggestions.c.replacement_id, _suggestions.c.basis).where(
                (_suggestions.c.tenant_id == tenant_id)
                & (_suggestions.c.record_id == record_id)
                & _suggestions.c.dismissed_at.is_(None)
            )
        )
        scopes = await conn.execute(
            select(_scopes.c.scope_type, _scopes.c.scope_value)
            .where((_scopes.c.tenant_id == tenant_id) & (_scopes.c.canonical_id == record_id))
            .order_by(_scopes.c.priority, _scopes.c.scope_value)
        )
        sources = await conn.execute(
            select(_ev.source_platform, _ev.audience_container, func.count())
            .select_from(linked)
            .where(mine)
            .group_by(_ev.source_platform, _ev.audience_container)
        )
        summary = redaction.serve(row["summary"], row["summary_redacted"])
        access = row["access_control"] or {}
        scope_rows = [{"scope_type": t, "scope_value": v} for t, v in scopes.all()]
        described = {
            "id": str(record_id),
            "context_type": row["context_type"],
            "severity": severity_name(row["severity"]),
            "summary": summary,
            "scope": [f"{r['scope_type']}:{r['scope_value']}" for r in scope_rows] or ["org"],
            "scope_rows": scope_rows,
            "is_locked": bool(row["is_locked"]),
            "audience": "public"
            if access.get("is_public")
            else f"{len(access.get('allowed_identity_ids') or [])} people",
            "sources": [{"platform": p, "container": c, "events": n} for p, c, n in sources.all()],
            "confidence_score": row["confidence_score"],
            "created_at": row["created_at"].isoformat(),
            "state": _state(row),
            "retired_at": row["retired_at"].isoformat() if row["retired_at"] else None,
            "retired_by": row["retired_by"],
            "retired_reason": row["retired_reason"],
            "replaced_by": str(row["replaced_by"]) if row["replaced_by"] else None,
            "supporting_events": {
                "count": count,
                "first": first.isoformat() if first else None,
                "last": last.isoformat() if last else None,
            },
            "suggested_replacements": [
                {"id": str(i), "replacement_id": str(r), "basis": b} for i, r, b in suggested.all()
            ],
            "updates": (
                await record_notes.updates(conn, tenant_id, [record_id], UPDATES_SHOWN, label=True)
            ).get(str(record_id), []),
        }
        if described["state"] == "live":
            note = (await record_notes.replacement_notes(conn, tenant_id, [record_id])).get(
                str(record_id)
            )
            if note:
                described["may_be_replaced_by"] = note
        await audit.record(
            conn,
            tenant_id,
            actor,
            "records.show",
            record_ids=[record_id],
            redacted=int(summary != row["summary"]),
            result_count=1,
        )
    return described


async def cited_messages(
    conn: AsyncConnection, tenant_id: UUID, chat_source: dict[str, Any]
) -> list[dict[str, Any]]:
    """The stored chat messages an outcome event cites, unredacted."""
    from checkcord.services.chat_messages import source_id

    channel = chat_source.get("channel_id")
    ids = chat_source.get("message_ids") or []
    if not channel or not ids:
        return []
    rows = await conn.execute(
        select(_ev.author, _ev.happened_at, _ev.content_clean)
        .where(
            (_ev.tenant_id == tenant_id)
            & (_ev.source_platform == chat_source.get("platform"))
            & _ev.source_id.in_([source_id(channel, m) for m in ids])
        )
        .order_by(_ev.happened_at)
    )
    return [
        {
            "author": (author or {}).get("display_name") or (author or {}).get("source_id"),
            "happened_at": happened_at.isoformat() if happened_at else None,
            "text": text,
        }
        for author, happened_at, text in rows
    ]


async def raw_sources(
    conn: AsyncConnection, tenant_id: UUID, sources: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out = []
    for src in sources:
        item = {
            **src,
            "id": src.get("event_id"),
            "platform": src.get("source_platform"),
            "text": src.get("content_clean"),
        }
        if src.get("chat_source"):
            item["messages"] = await cited_messages(conn, tenant_id, src["chat_source"])
        out.append(item)
    return out


async def raw(
    engine: AsyncEngine, tenant_id: UUID, record_id: UUID, actor: audit.Actor
) -> dict[str, Any] | None:
    """A record's unredacted text with its sources and, for chat, the messages they cite."""
    this = (_records.c.tenant_id == tenant_id) & (_records.c.id == record_id)
    async with tenant_context(engine, tenant_id, include_retired=True) as conn:
        row = (
            await conn.execute(select(_records.c.summary, _records.c.context_type).where(this))
        ).first()
        if row is None:
            return None
        events = await conn.execute(
            select(
                _ev.event_id,
                _ev.source_platform,
                _ev.source_type,
                _ev.content_clean,
                _ev.happened_at,
                _ev["metadata"]["chat_source"].label("chat_source"),
            )
            .select_from(_links.join(events_table, _ev.event_id == _links.c.event_id))
            .where(
                (_links.c.tenant_id == tenant_id)
                & (_links.c.canonical_id == record_id)
                & (_ev.tenant_id == tenant_id)
            )
            .order_by(_ev.happened_at)
        )
        sources = [
            {
                "event_id": str(r.event_id),
                "source_platform": r.source_platform,
                "source_type": r.source_type,
                "content_clean": r.content_clean,
                "happened_at": r.happened_at.isoformat() if r.happened_at else None,
                **({"chat_source": r.chat_source} if r.chat_source else {}),
            }
            for r in events
        ]
        found = {
            "id": str(record_id),
            "context_type": row.context_type,
            "summary": row.summary,
            "sources": await raw_sources(conn, tenant_id, sources),
        }
        await audit.record(
            conn,
            tenant_id,
            actor,
            "admin.raw_content",
            params={"node": f"context:{record_id}"},
            record_ids=[record_id],
            result_count=1,
        )
    return found


async def search(
    engine: AsyncEngine,
    tenant_id: UUID,
    actor: audit.Actor,
    *,
    repository: str | None = None,
    context_type: str | None = None,
    kind: Literal["decision", "reference"] | None = None,
    severity: SeverityName | None = None,
    state: State = "live",
    query: str | None = None,
    limit: int = 50,
    cursor: tuple[datetime, UUID] | None = None,
) -> tuple[list[dict[str, Any]], tuple[datetime, UUID] | None]:
    """Records newest first with redacted summaries. Text search matches the raw summary."""
    c = _records.c
    stmt = select(
        c.id,
        c.context_type,
        c.severity,
        c.summary,
        c.summary_redacted,
        c.held_connection,
        c.retired_at,
        c.replaced_by,
        c.confidence_score,
        c.created_at,
    ).where(c.tenant_id == tenant_id)
    if state == "held":
        stmt = stmt.where(c.held_connection.is_not(None))
    elif state == "retired":
        stmt = stmt.where(c.retired_at.is_not(None))
    else:
        stmt = stmt.where(c.held_connection.is_(None) & c.retired_at.is_(None))
    if repository:
        stmt = stmt.where(
            select(_scopes.c.canonical_id)
            .where(
                (_scopes.c.tenant_id == tenant_id)
                & (_scopes.c.canonical_id == c.id)
                & (_scopes.c.scope_type == "repo")
                & (_scopes.c.scope_value == repository)
            )
            .exists()
        )
    if context_type:
        stmt = stmt.where(c.context_type == context_type)
    if kind == "decision":
        stmt = stmt.where(decisions_or_blocking(_records))
    elif kind == "reference":
        stmt = stmt.where(~decisions_or_blocking(_records))
    if severity:
        stmt = stmt.where(c.severity == SEVERITIES[severity])
    if query:
        stmt = stmt.where(c.summary.ilike(f"%{query.strip()}%"))
    if cursor is not None:
        stmt = stmt.where(tuple_(c.created_at, c.id) < tuple_(*cursor))
    stmt = stmt.order_by(c.created_at.desc(), c.id.desc()).limit(limit + 1)
    async with tenant_context(
        engine, tenant_id, include_held=state == "held", include_retired=state == "retired"
    ) as conn:
        rows = list((await conn.execute(stmt)).mappings())
        more = len(rows) > limit
        rows = rows[:limit]
        served, redacted = redaction.serve_field([dict(r) for r in rows])
        await audit.record(
            conn,
            tenant_id,
            actor,
            "records.search",
            params={
                "repository": repository,
                "type": context_type,
                "kind": kind,
                "severity": severity,
                "state": state,
                "q": audit.search_text(query),
            },
            record_ids=[r["id"] for r in served],
            redacted=redacted,
            result_count=len(served),
        )
    items = [
        {
            "id": r["id"],
            "context_type": r["context_type"],
            "severity": severity_name(r["severity"]),
            "summary": r["summary"],
            "state": _state(r),
            "held_connection": r["held_connection"],
            "retired_at": r["retired_at"],
            "replaced_by": r["replaced_by"],
            "confidence_score": r["confidence_score"],
            "created_at": r["created_at"],
        }
        for r in served
    ]
    return items, ((rows[-1]["created_at"], rows[-1]["id"]) if more else None)


async def _rewrite(redis: Redis | None, tenant_id: UUID, why: str, record_id: UUID) -> None:
    from checkcord.services.audiences import rewrite_files

    await rewrite_files(redis, tenant_id, "records", why, [record_id])


async def _retire(
    conn: AsyncConnection,
    tenant_id: UUID,
    record_id: UUID,
    actor: audit.Actor,
    *,
    replaced_by: UUID | None,
    reason: str | None,
    params: dict[str, Any],
) -> str | None:
    c = _records.c
    this = (c.tenant_id == tenant_id) & (c.id == record_id)
    row = (await conn.execute(select(c.retired_at).where(this).with_for_update())).first()
    if row is None:
        return "not_found"
    if row.retired_at is not None:
        return "retired"
    if replaced_by is not None:
        if replaced_by == record_id:
            return "self"
        found = await conn.scalar(
            select(c.id).where(
                (c.tenant_id == tenant_id) & (c.id == replaced_by) & c.retired_at.is_(None)
            )
        )
        if found is None:
            return "replacement_not_found"
    await conn.execute(
        update(_records)
        .where(this)
        .values(
            retired_at=func.now(),
            retired_by=_who(actor),
            retired_reason=reason,
            replaced_by=replaced_by,
            updated_at=func.now(),
        )
    )
    await conn.execute(
        delete(_suggestions).where(
            (_suggestions.c.tenant_id == tenant_id)
            & (_suggestions.c.record_id == record_id)
            & _suggestions.c.dismissed_at.is_(None)
        )
    )
    await audit.record(
        conn,
        tenant_id,
        actor,
        "context.retired",
        params={"replaced_by": str(replaced_by) if replaced_by else None, **params},
        record_ids=[record_id] + ([replaced_by] if replaced_by else []),
    )
    return None


async def retire(
    engine: AsyncEngine,
    tenant_id: UUID,
    record_id: UUID,
    actor: audit.Actor,
    *,
    replaced_by: UUID | None = None,
    reason: str | None = None,
    redis: Redis | None = None,
) -> str | None:
    """None when retired; otherwise why not: not_found, retired, self, replacement_not_found."""
    async with tenant_context(engine, tenant_id, include_retired=True) as conn:
        error = await _retire(
            conn, tenant_id, record_id, actor, replaced_by=replaced_by, reason=reason, params={}
        )
    if error is None:
        await _rewrite(redis, tenant_id, "retired", record_id)
    return error


async def restore(
    engine: AsyncEngine,
    tenant_id: UUID,
    record_id: UUID,
    actor: audit.Actor,
    *,
    redis: Redis | None = None,
) -> str | None:
    """None when restored; otherwise not_found or not_retired."""
    c = _records.c
    this = (c.tenant_id == tenant_id) & (c.id == record_id)
    async with tenant_context(engine, tenant_id, include_retired=True) as conn:
        row = (await conn.execute(select(c.retired_at).where(this).with_for_update())).first()
        if row is None:
            return "not_found"
        if row.retired_at is None:
            return "not_retired"
        await conn.execute(
            update(_records)
            .where(this)
            .values(
                retired_at=None,
                retired_by=None,
                retired_reason=None,
                replaced_by=None,
                updated_at=func.now(),
            )
        )
        await audit.record(conn, tenant_id, actor, "context.restored", record_ids=[record_id])
    await _rewrite(redis, tenant_id, "restored", record_id)
    return None


async def suggest(
    conn: AsyncConnection,
    tenant_id: UUID,
    record_id: UUID,
    replacement_id: UUID,
    basis: Literal["stated", "model"],
    similarity: float | None,
) -> None:
    """Store an open suggestion that ``replacement_id`` replaces ``record_id``."""
    await conn.execute(
        insert(_suggestions)
        .values(
            tenant_id=tenant_id,
            record_id=record_id,
            replacement_id=replacement_id,
            basis=basis,
            similarity=similarity,
        )
        .on_conflict_do_nothing(constraint="uq_record_replacement_suggestions")
    )


async def suggestions(
    engine: AsyncEngine,
    tenant_id: UUID,
    actor: audit.Actor,
    *,
    limit: int = 50,
    cursor: tuple[datetime, UUID] | None = None,
) -> tuple[list[dict[str, Any]], tuple[datetime, UUID] | None]:
    """Open suggestions newest first, both records live, with redacted summaries."""
    s = _suggestions.c
    old = _records.alias("old")
    new = _records.alias("new")
    stmt = (
        select(
            s.id,
            s.basis,
            s.similarity,
            s.created_at,
            old.c.id.label("record_id"),
            old.c.summary.label("record_summary"),
            old.c.summary_redacted.label("record_summary_redacted"),
            old.c.created_at.label("record_created_at"),
            new.c.id.label("replacement_id"),
            new.c.summary.label("replacement_summary"),
            new.c.summary_redacted.label("replacement_summary_redacted"),
            new.c.created_at.label("replacement_created_at"),
        )
        .select_from(
            _suggestions.join(old, old.c.id == s.record_id).join(new, new.c.id == s.replacement_id)
        )
        .where((s.tenant_id == tenant_id) & s.dismissed_at.is_(None))
    )
    if cursor is not None:
        stmt = stmt.where(tuple_(s.created_at, s.id) < tuple_(*cursor))
    stmt = stmt.order_by(s.created_at.desc(), s.id.desc()).limit(limit + 1)
    async with tenant_context(engine, tenant_id) as conn:
        rows = [dict(r) for r in (await conn.execute(stmt)).mappings()]
        more = len(rows) > limit
        rows = rows[:limit]
        redacted = 0
        for r in rows:
            for side in ("record", "replacement"):
                raw = r.pop(f"{side}_summary")
                served = redaction.serve(raw, r.pop(f"{side}_summary_redacted"))
                redacted += int(served != raw)
                r[f"{side}_summary"] = served
        await audit.record(
            conn,
            tenant_id,
            actor,
            "records.suggestions",
            record_ids=[i for r in rows for i in (r["record_id"], r["replacement_id"])],
            redacted=redacted,
            result_count=len(rows),
        )
    return rows, ((rows[-1]["created_at"], rows[-1]["id"]) if more else None)


async def decide_suggestion(
    engine: AsyncEngine,
    tenant_id: UUID,
    suggestion_id: UUID,
    accept: bool,
    actor: audit.Actor,
    *,
    redis: Redis | None = None,
) -> str | None:
    """Accept (retire the old record, replaced by the new) or dismiss an open suggestion.

    None when done; otherwise not_found, or a reason from ``retire``.
    """
    s = _suggestions.c
    async with tenant_context(engine, tenant_id, include_retired=True) as conn:
        row = (
            await conn.execute(
                select(s.record_id, s.replacement_id, s.basis)
                .where((s.tenant_id == tenant_id) & (s.id == suggestion_id))
                .where(s.dismissed_at.is_(None))
                .with_for_update()
            )
        ).first()
        if row is None:
            return "not_found"
        params = {"suggestion": str(suggestion_id), "basis": row.basis}
        if not accept:
            await conn.execute(
                update(_suggestions)
                .where(s.id == suggestion_id)
                .values(dismissed_at=func.now(), dismissed_by=_who(actor))
            )
            await audit.record(
                conn,
                tenant_id,
                actor,
                "context.replacement_dismissed",
                params=params,
                record_ids=[row.record_id, row.replacement_id],
            )
            return None
        error = await _retire(
            conn,
            tenant_id,
            row.record_id,
            actor,
            replaced_by=row.replacement_id,
            reason=None,
            params=params,
        )
    if error is None:
        await _rewrite(redis, tenant_id, "retired", row.record_id)
    return error
