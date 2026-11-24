"""The audit log: one row per read of record content, admin action or failed sign-in."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import Select, func, insert, select, tuple_
from sqlalchemy.ext.asyncio import AsyncConnection

from checkcord.core import metrics
from checkcord.core.agent_text import removals
from checkcord.core.config import get_settings
from checkcord.storage.models import AuditLogEntry

MCP, API, CLI, SCIM = "mcp", "api", "cli", "scim"
CHANNELS = (MCP, API, CLI, SCIM)
SEARCH_TEXT_LIMIT = 200
PENDING_LINES = "checkcord.audit_pending_lines"
PR_CHECK = "pr.check_posted"
INSTRUCTIONS_REMOVED = "record.instructions_removed"
AGENT_READS = (
    "mcp.view_active_decisions",
    "mcp.search_conceptual_context",
    "mcp.get_dependency_flow",
    "graph.context",
    "graph.impact",
    "graph.active_decisions",
)

audit_logger = structlog.get_logger("checkcord.audit")
_table = AuditLogEntry.__table__
_c = _table.c

COLUMNS = (
    "id",
    "created_at",
    "tenant_id",
    "channel",
    "action",
    "user_id",
    "user_email",
    "key_id",
    "key_name",
    "params",
    "record_ids",
    "redacted_count",
    "result_count",
)


@dataclass(frozen=True)
class Actor:
    channel: str
    key_id: UUID | None = None
    key_name: str | None = None
    user_id: UUID | None = None
    user_email: str | None = None


CLI_ACTOR = Actor(channel=CLI)


def search_text(value: str | None) -> str | None:
    return value[:SEARCH_TEXT_LIMIT] if value else value


def jsonable(row: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, UUID) else v
        for k, v in row.items()
    }


def emit_lines(rows: Iterable[dict[str, Any]]) -> None:
    if not get_settings().audit_log_lines:
        return
    for row in rows:
        # Named explicitly too, for log setups without a logger-name processor.
        audit_logger.info("audit", logger="checkcord.audit", **jsonable(row))


async def record(
    conn: AsyncConnection,
    tenant_id: UUID,
    actor: Actor,
    action: str,
    *,
    params: dict[str, Any] | None = None,
    record_ids: Iterable[Any] = (),
    redacted: int = 0,
    result_count: int | None = None,
) -> None:
    """Insert one entry. Errors propagate so the caller's transaction, and its read, fail."""
    row: dict[str, Any] = {
        "id": uuid4(),
        "created_at": datetime.now(UTC),
        "tenant_id": tenant_id,
        "channel": actor.channel,
        "action": action,
        "user_id": actor.user_id,
        "user_email": actor.user_email,
        "key_id": actor.key_id,
        "key_name": actor.key_name,
        "params": json.loads(
            json.dumps({k: v for k, v in (params or {}).items() if v is not None}, default=str)
        ),
        "record_ids": list(dict.fromkeys(str(r) for r in record_ids if r is not None)),
        "redacted_count": redacted,
        "result_count": result_count,
    }
    try:
        await conn.execute(insert(_table).values(row))
    except Exception:
        metrics.AUDIT_WRITE_FAILURES.inc()
        raise
    pending = conn.info.get(PENDING_LINES) if isinstance(conn, AsyncConnection) else None
    if isinstance(pending, list):
        pending.append(row)
    else:
        emit_lines([row])


async def instructions_removed(
    conn: AsyncConnection, tenant_id: UUID, record_id: Any, text: str, before: str | None = None
) -> None:
    """Note that a record's stored text gained removed instructions; never the text."""
    count = removals(text)
    if count <= removals(before):
        return
    metrics.count_stage("record_instructions_removed_total")
    await record(
        conn,
        tenant_id,
        Actor(channel=API),
        INSTRUCTIONS_REMOVED,
        params={"count": count},
        record_ids=[record_id],
    )


@dataclass(frozen=True)
class Filters:
    since: datetime | None = None
    until: datetime | None = None
    user: str | None = None
    key: str | None = None
    action: str | None = None
    channel: str | None = None
    record_id: str | None = None

    def params(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _filtered(tenant_id: UUID, f: Filters) -> Select[Any]:
    stmt = select(*(_c[name] for name in COLUMNS)).where(_c.tenant_id == tenant_id)
    if f.since is not None:
        stmt = stmt.where(_c.created_at >= _aware(f.since))
    if f.until is not None:
        stmt = stmt.where(_c.created_at < _aware(f.until))
    if f.user:
        user_id = _as_uuid(f.user)
        stmt = stmt.where(
            _c.user_id == user_id
            if user_id
            else func.lower(_c.user_email) == f.user.strip().lower()
        )
    if f.key:
        key_id = _as_uuid(f.key)
        stmt = stmt.where(_c.key_id == key_id if key_id else _c.key_name == f.key)
    if f.action:
        stmt = stmt.where(_c.action == f.action)
    if f.channel:
        stmt = stmt.where(_c.channel == f.channel)
    if f.record_id:
        stmt = stmt.where(_c.record_ids.contains([f.record_id]))
    return stmt


def encode_cursor(row: dict[str, Any]) -> str:
    raw = f"{row['created_at'].isoformat()}|{row['id']}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        created_at, entry_id = raw.split("|", 1)
        return datetime.fromisoformat(created_at), UUID(entry_id)
    except (ValueError, UnicodeDecodeError) as err:
        raise ValueError("invalid cursor") from err


async def list_entries(
    conn: AsyncConnection,
    tenant_id: UUID,
    filters: Filters,
    *,
    limit: int,
    cursor: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Newest first; returns the page and the cursor for the next one, or None."""
    stmt = _filtered(tenant_id, filters)
    if cursor:
        created_at, entry_id = decode_cursor(cursor)
        stmt = stmt.where(tuple_(_c.created_at, _c.id) < tuple_(created_at, entry_id))
    stmt = stmt.order_by(_c.created_at.desc(), _c.id.desc()).limit(limit + 1)
    rows = [dict(r) for r in (await conn.execute(stmt)).mappings()]
    next_cursor = encode_cursor(rows[limit - 1]) if len(rows) > limit else None
    return rows[:limit], next_cursor


async def stream_entries(
    conn: AsyncConnection, tenant_id: UUID, filters: Filters
) -> AsyncIterator[dict[str, Any]]:
    """Oldest first, without loading the whole range into memory."""
    stmt = _filtered(tenant_id, filters).order_by(_c.created_at, _c.id)
    result = await conn.stream(stmt.execution_options(yield_per=500))
    async for row in result.mappings():
        yield dict(row)


def csv_row(row: dict[str, Any]) -> list[Any]:
    out = jsonable(row)
    out["params"] = json.dumps(row["params"], sort_keys=True)
    out["record_ids"] = " ".join(row["record_ids"])
    return [out[name] if out[name] is not None else "" for name in COLUMNS]
