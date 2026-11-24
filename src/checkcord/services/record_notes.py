"""Update excerpts and pending replacement notes served beside a record.

Both are read on the caller's own connection, so the same row-level security that
decided which records they see decides which updates and newer records they see.
"""

from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from checkcord.core import redaction
from checkcord.storage.models import (
    CanonicalContext,
    ContextEventLink,
    RecordReplacementSuggestion,
)
from checkcord.storage.postgres import event_audience, events_table

UPDATES_SERVED = 3
NOTE_SUMMARY_CHARS = 200

_links = ContextEventLink.__table__
_records = CanonicalContext.__table__
_suggestions = RecordReplacementSuggestion.__table__


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def audience_label(access_control: dict[str, Any]) -> str:
    if access_control.get("is_public"):
        return "public"
    readers = {
        str(u)
        for key in ("allowed_identity_ids", "admin_overrides")
        for u in access_control.get(key) or []
    }
    return f"{len(readers)} people"


async def updates(
    conn: AsyncConnection,
    tenant_id: UUID,
    ids: Sequence[Any],
    limit: int = UPDATES_SERVED,
    *,
    allows: Callable[[dict[str, Any]], bool] | None = None,
    label: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Up to ``limit`` update excerpts per record id, newest first. ``allows`` keeps only
    updates whose event it allows; ``label`` adds each update's audience."""
    keys = [UUID(str(i)) for i in ids if i]
    if not keys:
        return {}
    ev = events_table.c
    rank = (
        func.row_number()
        .over(
            partition_by=_links.c.canonical_id,
            order_by=(ev.happened_at.desc(), _links.c.event_id.desc()),
        )
        .label("rank")
    )
    inner = (
        select(
            _links.c.canonical_id,
            ev.happened_at,
            ev.source_platform,
            ev.source_type,
            _links.c.excerpt_redacted,
            ev.access_control,
            ev.access_allowed_ids,
            rank,
        )
        .select_from(
            _links.join(
                events_table,
                (ev.tenant_id == _links.c.tenant_id) & (ev.event_id == _links.c.event_id),
            )
        )
        .where(
            (_links.c.tenant_id == tenant_id)
            & _links.c.canonical_id.in_(keys)
            & _links.c.excerpt_redacted.is_not(None)
        )
        .subquery()
    )
    stmt = select(inner).order_by(inner.c.canonical_id, inner.c.rank)
    if allows is None:
        stmt = stmt.where(inner.c.rank <= limit)
    out: dict[str, list[dict[str, Any]]] = {}
    for r in await conn.execute(stmt):
        audience = event_audience(r._mapping)
        if allows is not None and not allows(audience):
            continue
        served = out.setdefault(str(r.canonical_id), [])
        if len(served) >= limit:
            continue
        update = {
            "happened_at": _iso(r.happened_at),
            "source": {"platform": r.source_platform, "type": r.source_type},
            "text": redaction.redact_regex(r.excerpt_redacted),
        }
        if label:
            update["audience"] = audience_label(audience)
        served.append(update)
    return out


async def replacement_notes(
    conn: AsyncConnection,
    tenant_id: UUID,
    ids: Sequence[Any],
    allows: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, dict[str, Any]]:
    """The newest live record with an open suggestion to replace each id, when readable.

    The newer record is read through ``conn``; ``allows`` further filters it by its
    access control for callers that serve an audience rather than one person.
    """
    keys = [UUID(str(i)) for i in ids if i]
    if not keys:
        return {}
    s, n = _suggestions.c, _records.c
    rows = await conn.execute(
        select(
            s.record_id,
            s.basis,
            n.id,
            n.created_at,
            n.summary,
            n.summary_redacted,
            n.access_control,
        )
        .select_from(_suggestions.join(_records, n.id == s.replacement_id))
        .where(
            (s.tenant_id == tenant_id)
            & s.record_id.in_(keys)
            & s.dismissed_at.is_(None)
            & n.retired_at.is_(None)
            & n.held_connection.is_(None)
        )
        .order_by(s.record_id, n.created_at.desc())
    )
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = str(r.record_id)
        if key in out or (allows is not None and not allows(r.access_control or {})):
            continue
        out[key] = {
            "record_id": str(r.id),
            "created_at": _iso(r.created_at),
            "basis": r.basis,
            "summary": redaction.serve(r.summary, r.summary_redacted)[:NOTE_SUMMARY_CHARS],
        }
    return out


async def annotate(
    conn: AsyncConnection,
    tenant_id: UUID,
    rows: list[dict[str, Any]],
    *,
    id_field: str = "id",
    limit: int = UPDATES_SERVED,
    allows: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """Add ``updates`` and, where one applies, ``may_be_replaced_by`` to each served row.
    ``allows`` filters both by access control, for callers that serve an audience."""
    ids = [r.get(id_field) for r in rows]
    found = await updates(conn, tenant_id, ids, limit, allows=allows)
    notes = await replacement_notes(conn, tenant_id, ids, allows)
    for r in rows:
        key = str(r.get(id_field))
        r["updates"] = found.get(key, [])
        if key in notes:
            r["may_be_replaced_by"] = notes[key]
    return rows
