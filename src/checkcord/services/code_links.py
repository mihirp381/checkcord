"""Links between records and the code symbols their text names."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from checkcord.core import redaction
from checkcord.models.enums import ASTEdgeStatus, Severity
from checkcord.services import record_notes
from checkcord.services.records import severity_name
from checkcord.storage.models import ASTContextEdge, ASTNode, CanonicalContext, ContextEventLink
from checkcord.storage.postgres import decisions_or_blocking, live_records, tenant_context

logger = structlog.get_logger(__name__)

RECORD_BATCH = 200
INSERT_BATCH = 1000

_edges = ASTContextEdge.__table__
_nodes = ASTNode.__table__
_records = CanonicalContext.__table__
_links = ContextEventLink.__table__


def is_camel_case(s: str) -> bool:
    if not s.isalnum():
        return False
    if len(s) < 3:
        return False
    has_upper = any(c.isupper() for c in s)
    has_lower = any(c.islower() for c in s)
    if not (has_upper and has_lower):
        return False
    uppers = [i for i, c in enumerate(s) if c.isupper()]
    if uppers == [0]:
        return False
    return True


def is_snake_case(s: str) -> bool:
    if "_" not in s:
        return False
    if len(s) < 3:
        return False
    if not all(c.isalnum() or c == "_" for c in s):
        return False
    if not any(c.isalnum() for c in s):
        return False
    return True


_TOKEN = re.compile(r"\b[a-zA-Z0-9_]+\b")
_BACKTICKED = re.compile(r"`([^`\n]+)`")
_DOTTED = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
_FILE_NAME = re.compile(r"[\w./-]*\w\.(?:py|tsx?|jsx?|go|java)\b")


def extract_code_symbols(text: str) -> set[str]:
    """Names in ``text`` that may be code symbols. File names are dropped: files are not
    nodes of the code graph."""
    if not text:
        return set()
    text = _FILE_NAME.sub(" ", text)
    symbols = {t for t in _TOKEN.findall(text) if is_camel_case(t) or is_snake_case(t)}
    for quoted in _BACKTICKED.findall(text):
        symbols.update(_TOKEN.findall(quoted))
    for dotted in _DOTTED.findall(text):
        *head, last = dotted.split(".")
        if len(last) > 1:
            symbols.add(last)
        symbols.update(p for p in head if is_camel_case(p) or is_snake_case(p))
    return symbols


async def _insert(
    conn: AsyncConnection, tenant_id: UUID, pairs: Iterable[tuple[UUID, UUID]]
) -> int:
    now = datetime.now(UTC)
    rows = [
        {
            "ast_node_id": node,
            "canonical_id": record,
            "tenant_id": tenant_id,
            "status": ASTEdgeStatus.ACTIVE,
            "last_accessed": now,
            "created_at": now,
            "updated_at": now,
        }
        for node, record in dict.fromkeys(pairs)
    ]
    made = 0
    for i in range(0, len(rows), INSERT_BATCH):
        stmt = (
            insert(_edges)
            .values(rows[i : i + INSERT_BATCH])
            .on_conflict_do_nothing(index_elements=["ast_node_id", "canonical_id"])
        )
        made += (await conn.execute(stmt)).rowcount or 0
    return made


async def link_record(
    conn: AsyncConnection, tenant_id: UUID, record_id: UUID, text: str | None
) -> int:
    """Link the record to every node of the tenant named in ``text``; returns links made."""
    symbols = sorted(extract_code_symbols(text or ""))
    if not symbols:
        return 0
    nodes = await conn.execute(
        select(_nodes.c.id).where(
            (_nodes.c.tenant_id == tenant_id) & _nodes.c.symbol_name.in_(symbols)
        )
    )
    return await _insert(conn, tenant_id, ((n, record_id) for (n,) in nodes))


async def link_repository(engine: AsyncEngine, tenant_id: UUID, repo_identifier: str) -> int:
    """Match every live record's text and excerpts against the repository's nodes."""
    async with tenant_context(engine, tenant_id) as conn:
        by_symbol: dict[str, list[UUID]] = {}
        for node_id, symbol in await conn.execute(
            select(_nodes.c.id, _nodes.c.symbol_name).where(
                (_nodes.c.tenant_id == tenant_id) & (_nodes.c.repo_identifier == repo_identifier)
            )
        ):
            by_symbol.setdefault(symbol, []).append(node_id)
    if not by_symbol:
        return 0
    made = 0
    after: UUID | None = None
    while True:
        async with tenant_context(engine, tenant_id) as conn:
            page = select(_records.c.id, _records.c.summary).where(
                (_records.c.tenant_id == tenant_id) & live_records(_records)
            )
            if after is not None:
                page = page.where(_records.c.id > after)
            rows = (await conn.execute(page.order_by(_records.c.id).limit(RECORD_BATCH))).all()
            if not rows:
                break
            after = rows[-1].id
            texts = {r.id: [r.summary or ""] for r in rows}
            for record_id, excerpt in await conn.execute(
                select(_links.c.canonical_id, _links.c.excerpt_redacted).where(
                    (_links.c.tenant_id == tenant_id)
                    & _links.c.canonical_id.in_(list(texts))
                    & _links.c.excerpt_redacted.is_not(None)
                )
            ):
                texts[record_id].append(excerpt)
            pairs = [
                (node, record_id)
                for record_id, parts in texts.items()
                for symbol in extract_code_symbols("\n".join(parts))
                for node in by_symbol.get(symbol, ())
            ]
            made += await _insert(conn, tenant_id, pairs)
        if len(rows) < RECORD_BATCH:
            break
    logger.info(
        "code_links.repository_linked", tenant_id=str(tenant_id), repo=repo_identifier, made=made
    )
    return made


NODE_RECORDS = 5
RECORD_TEXT_CHARS = 240


async def node_records(
    conn: AsyncConnection, tenant_id: UUID, node_ids: Iterable[UUID], limit: int = NODE_RECORDS
) -> dict[UUID, list[dict[str, Any]]]:
    """Up to ``limit`` live records linked to each node that the connection's reader may
    read: decision records (and blocking ones) first, then reference records; blocking first,
    then newest within each."""
    ids = list(node_ids)
    if not ids:
        return {}
    blocking = _records.c.severity == Severity.HARD_BLOCK.value
    rows = (
        await conn.execute(
            select(
                _edges.c.ast_node_id,
                _records.c.id,
                _records.c.context_type,
                _records.c.severity,
                _records.c.summary,
                _records.c.summary_redacted,
            )
            .select_from(_edges.join(_records, _records.c.id == _edges.c.canonical_id))
            .where(
                (_edges.c.tenant_id == tenant_id)
                & _edges.c.ast_node_id.in_(ids)
                & (_edges.c.status == ASTEdgeStatus.ACTIVE.value)
                & (_records.c.tenant_id == tenant_id)
                & live_records(_records)
            )
            .order_by(
                decisions_or_blocking(_records).desc(),
                blocking.desc(),
                _records.c.created_at.desc(),
                _records.c.id,
            )
        )
    ).all()
    found: dict[UUID, list[dict[str, Any]]] = {}
    for row in rows:
        served = found.setdefault(row.ast_node_id, [])
        if len(served) < limit:
            served.append(
                {
                    "id": str(row.id),
                    "type": row.context_type,
                    "severity": severity_name(row.severity),
                    "text": redaction.serve(row.summary, row.summary_redacted)[:RECORD_TEXT_CHARS],
                }
            )
    notes = await record_notes.replacement_notes(
        conn, tenant_id, list({r["id"] for served in found.values() for r in served})
    )
    for served in found.values():
        for r in served:
            if r["id"] in notes:
                r["may_be_replaced_by"] = notes[r["id"]]
    return found
