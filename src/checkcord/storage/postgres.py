"""The Postgres event and record store."""

from __future__ import annotations

import hashlib
import json
import typing
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql import text

from checkcord.core import metrics
from checkcord.core.audience import record_audience
from checkcord.core.redaction import StoredRedaction
from checkcord.models.common import AgentMetadata
from checkcord.models.event import CanonicalEvent


def custom_json_serializer(obj: typing.Any, **kwargs: typing.Any) -> str:
    """Custom JSON serializer for UUIDs."""

    def default_encoder(o: typing.Any) -> typing.Any:
        if isinstance(o, UUID):
            return str(o)
        raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")

    return json.dumps(obj, default=default_encoder, **kwargs)


def create_checkcord_engine(url: str, **kwargs: typing.Any) -> AsyncEngine:
    """AsyncEngine whose JSON serializer handles UUIDs."""
    from sqlalchemy.ext.asyncio import create_async_engine

    kwargs.setdefault("json_serializer", custom_json_serializer)
    return create_async_engine(url, **kwargs)


def normalize_file_path(file_path: str, repo_identifier: str) -> str:
    """Normalize file_path to be relative to the repository root.
    If it is absolute, tries to find the repository name (from repo_identifier)
    in the path and strips the leading directory prefix.
    Also normalizes path separators to forward slashes.
    """
    if not file_path:
        return file_path

    normalized = file_path.replace("\\", "/")

    # If it's absolute, extract relative portion
    is_absolute = normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":")

    if is_absolute:
        repo_name = repo_identifier.split("/")[-1] if repo_identifier else ""
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]

        if repo_name:
            search_str = f"/{repo_name}/"
            if search_str in normalized:
                _, _, relative = normalized.partition(search_str)
                return relative
            elif normalized.endswith(f"/{repo_name}"):
                return ""
            parts = normalized.split("/")
            if repo_name in parts:
                idx = parts.index(repo_name)
                return "/".join(parts[idx + 1 :])

    # If not absolute or fallback failed, clean it up (remove leading dot/slashes)
    while normalized.startswith("./"):
        normalized = normalized[2:]
    while normalized.startswith("/"):
        normalized = normalized[1:]

    return normalized


logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Table definition
# ---------------------------------------------------------------------------

metadata_obj = MetaData()

events_table = Table(
    "events",
    metadata_obj,
    # ── Identifiers ──────────────────────────────────────────────────────
    Column("event_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", PG_UUID(as_uuid=True), nullable=False, index=True),
    Column("parent_event_id", PG_UUID(as_uuid=True), nullable=True),
    Column("fingerprint", String, nullable=False),
    Column("schema_version", Integer, nullable=False, server_default="1"),
    # ── Source origin ────────────────────────────────────────────────────
    Column("source_platform", String, nullable=False),
    Column("source_type", String, nullable=False),
    Column("event_action", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("version", Integer, nullable=False),
    # ── Temporal ─────────────────────────────────────────────────────────
    Column("happened_at", DateTime(timezone=True), nullable=False),
    Column("ingested_at", DateTime(timezone=True), nullable=False),
    # ── Payload ──────────────────────────────────────────────────────────
    Column("content_raw", Text, nullable=False),
    Column("content_clean", Text, nullable=False),
    # ── Governance & state ───────────────────────────────────────────────
    Column("signal_status", String, nullable=False, server_default="unprocessed"),
    # ── Nested JSONB blobs ───────────────────────────────────────────────
    Column("author", JSONB, nullable=False),
    Column("access_control", JSONB, nullable=False),
    Column("audience_container", String, nullable=True),
    Column("connection", String, nullable=True),
    Column("held_connection", String, nullable=True),
    Column("source_raw_payload", JSONB, nullable=False, server_default="{}"),
    Column("code_metadata", JSONB, nullable=True),
    Column("task_metadata", JSONB, nullable=True),
    Column("document_metadata", JSONB, nullable=True),
    Column("agent_metadata", JSONB, nullable=True),
    Column("incident_metadata", JSONB, nullable=True),
    Column("meeting_metadata", JSONB, nullable=True),
    Column("chat_metadata", JSONB, nullable=True),
    Column("entities", JSONB, nullable=False, server_default="[]"),
    Column("metadata", JSONB, nullable=False, server_default="{}"),
    # ── Top-level GIN-indexed arrays (no monolithic JSONB) ───────────────
    Column(
        "access_allowed_ids",
        ARRAY(PG_UUID(as_uuid=True)),
        nullable=False,
        server_default="{}",
    ),
    Column(
        "code_files_changed",
        ARRAY(String),
        nullable=False,
        server_default="{}",
    ),
    # ── Pipeline progress (storage/event_pipeline.py) ────────────────────
    Column("pipeline_state", String, nullable=False, server_default="done"),
    Column("pipeline_attempts", Integer, nullable=False, server_default="0"),
    Column("pipeline_started_at", DateTime(timezone=True), nullable=True),
    Column("pipeline_next_attempt_at", DateTime(timezone=True), nullable=True),
    Column("pipeline_last_error", Text, nullable=True),
    # NOTE: The unique constraint on (tenant_id, fingerprint, happened_at) is
    # defined in the Alembic migration as a partial index, because partitioned
    # tables require the partition key in any unique constraint.  We do NOT
    # duplicate it here — this Table object is used only for query building.
)


# ---------------------------------------------------------------------------
# Context Management
# ---------------------------------------------------------------------------


@asynccontextmanager  # Decorator to manage the context of the connection for RLS
async def tenant_context(
    engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID | None = None,
    *,
    include_held: bool = False,
    include_retired: bool = False,
) -> AsyncGenerator[AsyncConnection, None]:
    """Provide an AsyncConnection with the tenant (and optionally per-user) context set for RLS.

    Sets ``app.current_tenant`` with ``SET LOCAL`` so everything in the block
    is isolated to that tenant. ``user_id``, when given, also sets
    ``app.current_user_id``, which the per-user ``RESTRICTIVE`` policies on
    ``canonical_contexts`` and ``events`` check. Without it those policies do
    not apply; only internal workers may omit it.

    ``include_held`` sets ``app.include_held``: only then can the block read or
    write events and records held for review. ``include_retired`` sets
    ``app.include_retired``: only then can the block read or change retired records.
    """
    from checkcord.storage import audit

    lines: list[dict[str, Any]] = []
    async with engine.begin() as conn:
        await conn.execute(text(f"SET LOCAL app.current_tenant = '{tenant_id}'"))
        if user_id is not None:
            await conn.execute(text(f"SET LOCAL app.current_user_id = '{user_id}'"))
        if include_held:
            await conn.execute(text("SET LOCAL app.include_held = 'true'"))
        if include_retired:
            await conn.execute(text("SET LOCAL app.include_retired = 'true'"))
        # Held directly: conn.info raises once the connection is invalidated.
        info = conn.info
        info[audit.PENDING_LINES] = lines
        try:
            yield conn
        finally:
            info.pop(audit.PENDING_LINES, None)
    audit.emit_lines(lines)


DECISION_TYPES = ("adr", "constraint", "business_requirement", "risk")
REFERENCE_TYPE = "general"
BLOCKING = "hard_block"


def decisions_or_blocking(table: Any) -> Any:
    """Decision records, and any record an admin marked blocking."""
    return table.c.context_type.in_(DECISION_TYPES) | (table.c.severity == BLOCKING)


def live_records(table: Any) -> Any:
    """Records whose code links count: not retired and not held. Purged records are gone."""
    return table.c.retired_at.is_(None) & table.c.held_connection.is_(None)


def event_audience(event: Mapping[str, Any]) -> dict[str, Any]:
    """An ``events_table`` row's audience in ``AccessControl`` shape."""
    return {
        **(event.get("access_control") or {}),
        "allowed_identity_ids": event.get("access_allowed_ids") or [],
    }


def aggregate_access_control(
    events: list[dict[str, Any]], base_event_id: UUID | None = None
) -> dict[str, Any]:
    """Record-level access_control from rows of ``events_table``, oldest link first.

    The record takes the audience of its base event (the first row when
    ``base_event_id`` is not among them); see ``record_audience``.
    """
    shaped = [(event.get("event_id"), event_audience(event)) for event in events]
    base = next((ac for eid, ac in shaped if eid == base_event_id), None)
    if base is None and shaped:
        base = shaped[0][1]
    return record_audience(base, [ac for _, ac in shaped])


async def _apply_container_audiences(conn: AsyncConnection, rows: list[dict[str, Any]]) -> None:
    """Give events the stored audience of their container, which a refresh may have changed
    since the event was built. The row lock orders this against a running refresh."""
    from sqlalchemy import select

    from checkcord.storage.models import AudienceContainer

    containers = AudienceContainer.__table__.c
    for row in rows:
        if not row.get("audience_container"):
            continue
        stored = (
            await conn.execute(
                select(containers.is_public, containers.member_ids)
                .where(
                    (containers.tenant_id == row["tenant_id"])
                    & (containers.platform == row["source_platform"])
                    & (containers.container == row["audience_container"])
                )
                .with_for_update(read=True)
            )
        ).first()
        if stored is None:
            continue
        is_public, member_ids = stored
        row["access_control"] = {
            **row["access_control"],
            "is_public": bool(is_public),
            "visibility": "public" if is_public else "private",
        }
        row["access_allowed_ids"] = [] if is_public else list(member_ids)


async def _apply_connection_audiences(conn: AsyncConnection, rows: list[dict[str, Any]]) -> None:
    from checkcord.services.connection_audiences import apply_at_ingest

    await apply_at_ingest(conn, rows)


def review_key(row: dict[str, Any]) -> str | None:
    """The connection whose review state decides whether an event row is held."""
    if row.get("connection"):
        return str(row["connection"])
    if row.get("source_platform") == "github" and row.get("audience_container"):
        return f"github:{row['audience_container']}"
    return None


def source_id_hash(source_id: str) -> str:
    return hashlib.sha256(source_id.encode("utf-8")).hexdigest()


async def was_removed(conn: AsyncConnection, row: Mapping[str, Any]) -> bool:
    from sqlalchemy import select

    from checkcord.storage.models import RemovedSource

    removed = RemovedSource.__table__.c
    found = await conn.execute(
        select(removed.tenant_id).where(
            (removed.tenant_id == row["tenant_id"])
            & (removed.source_platform == row["source_platform"])
            & (removed.source_id_hash == source_id_hash(row["source_id"]))
        )
    )
    return found.first() is not None


async def _apply_review_holds(conn: AsyncConnection, rows: list[dict[str, Any]]) -> None:
    """Hold events whose connection is in review. A connection seen for the first time gets
    a row with the configured default. The share lock orders this against a release."""
    from sqlalchemy import select, tuple_

    from checkcord.core.config import get_settings
    from checkcord.storage.models import ConnectionReview

    keyed = [(row, review_key(row)) for row in rows]
    keys = sorted({(row["tenant_id"], key) for row, key in keyed if key}, key=str)
    for row, _ in keyed:
        row["held_connection"] = None
    if not keys:
        return
    reviews = ConnectionReview.__table__
    default = get_settings().review_new_connections
    await conn.execute(
        pg_insert(reviews)
        .values([{"tenant_id": t, "connection": k, "in_review": default} for t, k in keys])
        .on_conflict_do_nothing()
    )
    found = await conn.execute(
        select(reviews.c.tenant_id, reviews.c.connection)
        .where(tuple_(reviews.c.tenant_id, reviews.c.connection).in_(keys) & reviews.c.in_review)
        .with_for_update(read=True)
    )
    held = {(t, k) for t, k in found.all()}
    for row, key in keyed:
        if (row["tenant_id"], key) in held:
            row["held_connection"] = key


@asynccontextmanager
async def app_bypass_rls_context(
    engine: AsyncEngine | AsyncConnection,
) -> AsyncGenerator[AsyncConnection, None]:
    """Provide an AsyncConnection with RLS bypass enabled for initial auth/installation lookups."""
    if isinstance(engine, AsyncConnection):
        await engine.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        try:
            yield engine
        finally:
            await engine.execute(text("SET LOCAL app.bypass_rls = 'false'"))
    else:
        from checkcord.storage import audit

        lines: list[dict[str, Any]] = []
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            info = conn.info
            info[audit.PENDING_LINES] = lines
            try:
                yield conn
            finally:
                info.pop(audit.PENDING_LINES, None)
                await conn.execute(text("SET LOCAL app.bypass_rls = 'false'"))
        audit.emit_lines(lines)


_TENANT_REGISTRY_TABLES = (
    "api_keys",
    "github_installations",
    "jira_installations",
    "linear_installations",
    "notion_installations",
    "slack_installations",
    "msteams_installations",
    "msteams_meeting_installations",
    "zoom_installations",
    "datadog_installations",
    "sentry_installations",
    "audience_containers",
)


async def list_tenant_ids(engine: AsyncEngine) -> list[UUID]:
    """Tenants known to this deployment: keys, installations and configured defaults."""
    from checkcord.core.config import get_settings

    query = text(" UNION ".join(f"SELECT tenant_id FROM {t}" for t in _TENANT_REGISTRY_TABLES))
    async with app_bypass_rls_context(engine) as conn:
        tenants = {row[0] for row in (await conn.execute(query)).fetchall()}

    settings = get_settings()
    if settings.default_tenant_id is not None:
        tenants.add(settings.default_tenant_id)
    return sorted(tenants, key=str)


_RRF_K = 60


def _fuse_rrf(
    vector_rows: list[dict[str, Any]], lexical_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion: ``score(id) = sum(1 / (k + rank_i))`` over every
    1-indexed ranked list a candidate appears in.

    A candidate in only one list gets only that list's contribution.
    ``k=60`` is the usual default.
    """
    scores: dict[Any, float] = {}
    rows_by_id: dict[Any, dict[str, Any]] = {}

    for rank, row in enumerate(vector_rows, start=1):
        scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (_RRF_K + rank)
        rows_by_id[row["id"]] = row

    for rank, row in enumerate(lexical_rows, start=1):
        scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (_RRF_K + rank)
        rows_by_id.setdefault(row["id"], row)

    fused_ids = sorted(scores, key=lambda cid: scores[cid], reverse=True)
    return [rows_by_id[cid] for cid in fused_ids]


# Scope-tier priority; more specific wins. 'org' is never a stored row,
# so only these three tiers appear in context_repo_scopes.scope_type.
_SCOPE_TYPE_PRIORITIES: dict[str, int] = {"team": 1, "repo": 2, "path": 3}
_DEFAULT_SCOPE_PRIORITY = 2  # unrecognized scope_type falls back to 'repo'-equivalent


def _priority_for_scope_type(scope_type: str) -> int:
    return _SCOPE_TYPE_PRIORITIES.get(scope_type, _DEFAULT_SCOPE_PRIORITY)


def _scope_filter(
    tenant_id: UUID,
    repo_identifier: str | None = None,
    team_identifier: str | None = None,
    path: str | None = None,
) -> Any:
    """Predicate for records that are org-global or scoped to a matching tier.

    A record is eligible if it has no ``context_repo_scopes`` rows or at
    least one row whose ``(scope_type, scope_value)`` matches a supplied
    identifier. With no identifiers only org-global records are eligible:
    "no scope specified" never means every team's, repo's or path's rules.

    This answers eligibility only. When several rows match, callers pick the
    winning scope from ``priority``/``created_at`` on the rows.
    """
    from sqlalchemy import exists, or_, select

    from checkcord.storage.models import CanonicalContext as DBCanonicalContext
    from checkcord.storage.models import ContextRepoScope as DBContextRepoScope

    scope_rows = select(1).where(
        (DBContextRepoScope.__table__.c.canonical_id == DBCanonicalContext.__table__.c.id)
        & (DBContextRepoScope.__table__.c.tenant_id == tenant_id)
    )
    is_org_global = ~exists(scope_rows)

    match_clauses = []
    if repo_identifier is not None:
        match_clauses.append(
            (DBContextRepoScope.__table__.c.scope_type == "repo")
            & (DBContextRepoScope.__table__.c.scope_value == repo_identifier)
        )
    if team_identifier is not None:
        match_clauses.append(
            (DBContextRepoScope.__table__.c.scope_type == "team")
            & (DBContextRepoScope.__table__.c.scope_value == team_identifier)
        )
    if path is not None:
        match_clauses.append(
            (DBContextRepoScope.__table__.c.scope_type == "path")
            & (DBContextRepoScope.__table__.c.scope_value == path)
        )

    if not match_clauses:
        return is_org_global

    matches_any = exists(scope_rows.where(or_(*match_clauses)))
    return is_org_global | matches_any


# ---------------------------------------------------------------------------
# Store implementation
# ---------------------------------------------------------------------------


class PostgresEventStore:
    """Postgres storage for events and records; callers pass a ``tenant_context`` connection."""

    def __init__(self, bind: AsyncEngine | AsyncConnection) -> None:
        self._bind = bind

    # -- public API --------------------------------------------------------

    async def save_event(self, event: CanonicalEvent, pipeline_pending: bool = False) -> bool:
        """Insert a single event, returning *True* on success.

        Duplicate fingerprints (per tenant) are silently ignored and
        the method returns *False*. ``pipeline_pending`` saves the row as
        waiting for the pipeline work in ``storage/event_pipeline.py``. An event
        with an ``audience_container`` gets that container's stored audience. An
        event from a connection in review is saved held, which needs ``include_held``.
        """
        row = self._to_insert_dict(event)
        if pipeline_pending:
            now = datetime.now(UTC)
            row.update(
                pipeline_state="pending", pipeline_started_at=now, pipeline_next_attempt_at=now
            )

        async def _insert(conn: AsyncConnection) -> bool:
            if await was_removed(conn, row):
                metrics.EVENTS_SKIPPED_REMOVED.labels(row["source_platform"]).inc()
                return False
            await _apply_container_audiences(conn, [row])
            await _apply_connection_audiences(conn, [row])
            await _apply_review_holds(conn, [row])
            stmt = (
                pg_insert(events_table)
                .values(row)
                .on_conflict_do_nothing(
                    index_elements=["tenant_id", "fingerprint", "happened_at"],
                )
            )
            result = await conn.execute(stmt)
            return (result.rowcount or 0) > 0

        if isinstance(self._bind, AsyncConnection):
            inserted = await _insert(self._bind)
        else:
            async with self._bind.begin() as conn:
                inserted = await _insert(conn)

        logger.debug(
            "save_event",
            event_id=str(event.event_id),
            tenant_id=str(event.tenant_id),
            inserted=inserted,
        )
        return inserted

    async def update_agent_metadata(
        self,
        tenant_id: UUID,
        event_id: UUID,
        agent_metadata: AgentMetadata,
    ) -> bool:
        """Update the agent_metadata column for one event."""
        stmt = (
            update(events_table)
            .where(events_table.c.event_id == event_id)
            .where(events_table.c.tenant_id == tenant_id)
            .values(agent_metadata=agent_metadata.model_dump(mode="python"))
        )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            updated: bool = (result.rowcount or 0) > 0
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                updated = (result.rowcount or 0) > 0

        logger.debug(
            "update_agent_metadata",
            event_id=str(event_id),
            tenant_id=str(tenant_id),
            updated=updated,
        )
        return updated

    async def update_admin_overrides(
        self,
        tenant_id: UUID,
        event_id: UUID,
        admin_overrides: list[UUID],
    ) -> bool:
        """Update the admin_overrides field inside the access_control JSONB block for one event."""
        from sqlalchemy import cast, func
        from sqlalchemy.dialects.postgresql import JSONB

        json_val = [str(uid) for uid in admin_overrides]
        stmt = (
            update(events_table)
            .where(events_table.c.event_id == event_id)
            .where(events_table.c.tenant_id == tenant_id)
            .values(
                access_control=func.jsonb_set(
                    events_table.c.access_control,
                    cast(["admin_overrides"], ARRAY(String)),
                    cast(json_val, JSONB),
                )
            )
        )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            updated: bool = (result.rowcount or 0) > 0
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                updated = (result.rowcount or 0) > 0

        logger.debug(
            "update_admin_overrides",
            event_id=str(event_id),
            tenant_id=str(tenant_id),
            admin_overrides=[str(uid) for uid in admin_overrides],
            updated=updated,
        )
        return updated

    async def upsert_ledger_entry(
        self,
        tenant_id: UUID,
        scope_type: str,
        scope_id: str,
        key: str,
        value: dict[str, Any],
        agent_session_id: str | None = None,
        event_id: UUID | None = None,
    ) -> bool:
        """Upsert(insert or update) a ledger entry for a given scope and key."""
        from uuid import uuid4

        from checkcord.storage.models import AgentLedger as DBAgentLedger

        row = {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "key": key,
            "value": value,
            "agent_session_id": agent_session_id,
            "event_id": event_id,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        stmt = (
            pg_insert(DBAgentLedger.__table__)
            .values(row)
            .on_conflict_do_update(
                constraint="uq_agent_ledgers_scope_key",
                set_={
                    "value": value,
                    "agent_session_id": agent_session_id,
                    "event_id": event_id,
                    "updated_at": datetime.now(UTC),
                },
            )
        )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            success = (result.rowcount or 0) > 0
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                success = (result.rowcount or 0) > 0
        return success

    async def get_ledger_entry(
        self,
        tenant_id: UUID,
        scope_type: str,
        scope_id: str,
        key: str,
    ) -> dict[str, Any] | None:
        """Fetch a single ledger entry."""
        from checkcord.storage.models import AgentLedger as DBAgentLedger

        stmt = DBAgentLedger.__table__.select().where(
            (DBAgentLedger.__table__.c.tenant_id == tenant_id)
            & (DBAgentLedger.__table__.c.scope_type == scope_type)
            & (DBAgentLedger.__table__.c.scope_id == scope_id)
            & (DBAgentLedger.__table__.c.key == key)
        )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            row = result.mappings().fetchone()
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                row = result.mappings().fetchone()

        return dict(row) if row is not None else None

    async def get_ledger_entries(
        self,
        tenant_id: UUID,
        scope_type: str | None = None,
        scope_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch ledger entries matching the given scope parameters."""
        from checkcord.storage.models import AgentLedger as DBAgentLedger

        stmt = DBAgentLedger.__table__.select().where(
            DBAgentLedger.__table__.c.tenant_id == tenant_id
        )

        if scope_type is not None:
            stmt = stmt.where(DBAgentLedger.__table__.c.scope_type == scope_type)
        if scope_id is not None:
            stmt = stmt.where(DBAgentLedger.__table__.c.scope_id == scope_id)

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            rows = result.mappings().fetchall()
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                rows = result.mappings().fetchall()

        return [dict(r) for r in rows]

    async def search_skills(
        self,
        tenant_id: UUID,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search skills matching name or description, or return all if no query."""
        from checkcord.storage.models import Skill as DBSkill

        stmt = DBSkill.__table__.select().where(DBSkill.__table__.c.tenant_id == tenant_id)

        if query:
            pattern = f"%{query}%"
            stmt = stmt.where(
                DBSkill.__table__.c.name.ilike(pattern)
                | DBSkill.__table__.c.description.ilike(pattern)
            )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            rows = result.mappings().fetchall()
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                rows = result.mappings().fetchall()

        return [dict(r) for r in rows]

    async def save_canonical_context(
        self,
        tenant_id: UUID,
        summary: str,
        confidence_score: float,
        embedding: list[float] | None = None,
        id: UUID | None = None,
        access_control: dict[str, Any] | None = None,
        scope_repo_identifier: str | None = None,
        context_type: str | None = None,
        base_event_id: UUID | None = None,
        held_connection: str | None = None,
        redaction: StoredRedaction | None = None,
    ) -> UUID:
        """Insert or update a canonical context, returning its ID.

        ``redaction`` is the stored redacted form of ``summary``. Without it the
        row has none and reads serve the regex pass over ``summary``.

        ``held_connection`` is set only at creation: a record made from a held
        event is held under the same connection.

        ``context_type`` is set only at creation (``None`` means ``"general"``)
        and is left out of the conflict update, so a retry for the same ``id``
        cannot reclassify a record. ``base_event_id`` is the event the record
        was made from and takes its audience from.

        ``access_control`` should come from the originating event. When omitted
        the fail-closed default applies (not public, empty allow-list).

        ``scope_repo_identifier`` should come from the originating event's
        repository; ``None`` leaves the record org-global with no scope rows, a
        repo string gives it exactly one. A retry for an existing ``id`` replaces
        the row set with the value from this call. Widening a record to a second
        repo is a human action through ``update_context_scope``.

        ``content_hash`` (SHA-256 over ``summary``) and ``last_verified_at`` are
        recomputed on every write, since this is the only write path for
        ``summary``.
        """
        import hashlib
        import uuid

        from sqlalchemy import delete

        from checkcord.storage.models import CanonicalContext as DBCanonicalContext
        from checkcord.storage.models import ContextRepoScope as DBContextRepoScope
        from checkcord.storage.models import _default_access_control

        context_id = id or uuid.uuid4()
        resolved_access_control = (
            access_control if access_control is not None else _default_access_control()
        )
        content_hash = hashlib.sha256(summary.encode("utf-8")).hexdigest()
        now = datetime.now(UTC)
        resolved_context_type = context_type or "general"
        redacted = {
            "summary_redacted": redaction.text if redaction else None,
            "redaction_version": redaction.version if redaction else None,
            "redaction_pending": redaction.pending if redaction else False,
        }
        row = {
            "id": context_id,
            "tenant_id": tenant_id,
            "summary": summary,
            **redacted,
            "confidence_score": confidence_score,
            "embedding": embedding,
            "access_control": resolved_access_control,
            "content_hash": content_hash,
            "context_type": resolved_context_type,
            "base_event_id": base_event_id,
            "held_connection": held_connection,
            "last_verified_at": now,
            "created_at": now,
            "updated_at": now,
        }
        stmt = (
            pg_insert(DBCanonicalContext.__table__)
            .values(row)
            .on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "summary": summary,
                    **redacted,
                    "confidence_score": confidence_score,
                    "embedding": embedding,
                    "access_control": resolved_access_control,
                    "content_hash": content_hash,
                    "last_verified_at": now,
                    "updated_at": now,
                },
            )
        )
        delete_scope_stmt = delete(DBContextRepoScope.__table__).where(
            DBContextRepoScope.__table__.c.canonical_id == context_id
        )
        insert_scope_stmt = pg_insert(DBContextRepoScope.__table__).values(
            canonical_id=context_id,
            scope_type="repo",
            scope_value=scope_repo_identifier,
            priority=_priority_for_scope_type("repo"),
            tenant_id=tenant_id,
            created_at=now,
        )

        async def _apply(conn: AsyncConnection) -> None:
            await conn.execute(stmt)
            await conn.execute(delete_scope_stmt)
            if scope_repo_identifier is not None:
                await conn.execute(insert_scope_stmt)

        if isinstance(self._bind, AsyncConnection):
            await _apply(self._bind)
        else:
            async with self._bind.begin() as conn:
                await _apply(conn)

        return context_id

    async def link_context_to_event(
        self,
        tenant_id: UUID,
        canonical_id: UUID,
        event_id: UUID,
        excerpt: StoredRedaction | None = None,
    ) -> bool:
        """Link a canonical context to a raw event in the junction table.

        ``excerpt`` is the redacted update text kept for a later event that agrees
        with the record. On a new link, recomputes the record's ``access_control``
        (see ``recompute_context_access_control``) in the same transaction.
        """
        from checkcord.storage.models import ContextEventLink as DBContextEventLink

        row = {
            "canonical_id": canonical_id,
            "event_id": event_id,
            "tenant_id": tenant_id,
            "created_at": datetime.now(UTC),
            "excerpt_redacted": excerpt.text if excerpt else None,
            "excerpt_version": excerpt.version if excerpt else None,
            "excerpt_pending": excerpt.pending if excerpt else False,
        }
        stmt = (
            pg_insert(DBContextEventLink.__table__)
            .values(row)
            .on_conflict_do_nothing(
                index_elements=["canonical_id", "event_id"],
            )
        )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            inserted = (result.rowcount or 0) > 0
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                inserted = (result.rowcount or 0) > 0

        if inserted:
            await self.recompute_context_access_control(tenant_id, canonical_id)

        return inserted

    async def recompute_context_access_control(
        self, tenant_id: UUID, canonical_id: UUID
    ) -> dict[str, Any] | None:
        """Store a record's ``access_control``: its base event's audience, with the
        ``admin_overrides`` of every linked event.

        Locks the record row with ``SELECT ... FOR UPDATE`` before reading its
        linked events, so two concurrent links cannot each miss the other's
        contribution. Returns the new value, or None when the record is not found.
        """
        from sqlalchemy import select

        from checkcord.storage.models import CanonicalContext as DBCanonicalContext
        from checkcord.storage.models import ContextEventLink as DBContextEventLink

        contexts = DBCanonicalContext.__table__
        links = DBContextEventLink.__table__
        this_context = (contexts.c.id == canonical_id) & (contexts.c.tenant_id == tenant_id)
        lock_stmt = select(contexts.c.base_event_id).where(this_context).with_for_update()
        linked_events_stmt = (
            select(
                events_table.c.event_id,
                events_table.c.access_control,
                events_table.c.access_allowed_ids,
            )
            .select_from(links.join(events_table, links.c.event_id == events_table.c.event_id))
            .where(
                (links.c.tenant_id == tenant_id)
                & (links.c.canonical_id == canonical_id)
                & (events_table.c.tenant_id == tenant_id)
            )
            .order_by(links.c.created_at, links.c.event_id)
            # Holds an audience refresh off these events until this record is written.
            .with_for_update(read=True, of=events_table)
        )

        async def _apply(conn: AsyncConnection) -> dict[str, Any] | None:
            base_event_id = (await conn.execute(lock_stmt)).first()
            if base_event_id is None:
                return None
            linked = await conn.execute(linked_events_stmt)
            access_control = aggregate_access_control(
                [dict(r) for r in linked.mappings().fetchall()], base_event_id[0]
            )
            await conn.execute(
                update(contexts).where(this_context).values(access_control=access_control)
            )
            return access_control

        if isinstance(self._bind, AsyncConnection):
            return await _apply(self._bind)
        async with self._bind.begin() as conn:
            return await _apply(conn)

    async def recompute_access_for_event(self, tenant_id: UUID, event_id: UUID) -> int:
        """Recompute every record linked to an event. Returns how many were updated."""
        from sqlalchemy import select

        from checkcord.storage.models import ContextEventLink as DBContextEventLink

        links = DBContextEventLink.__table__
        stmt = select(links.c.canonical_id).where(
            (links.c.tenant_id == tenant_id) & (links.c.event_id == event_id)
        )
        if isinstance(self._bind, AsyncConnection):
            canonical_ids = (await self._bind.execute(stmt)).scalars().all()
        else:
            async with self._bind.begin() as conn:
                canonical_ids = (await conn.execute(stmt)).scalars().all()
        updated = 0
        for canonical_id in canonical_ids:
            if await self.recompute_context_access_control(tenant_id, canonical_id) is not None:
                updated += 1
        return updated

    async def update_context_scope(
        self,
        tenant_id: UUID,
        canonical_id: UUID,
        repo_identifiers: list[str] | None,
        is_locked: bool,
        changed_by: str,
        scope_rows: list[dict[str, str]] | None = None,
    ) -> dict[str, Any] | None:
        """Replace a record's scope set and lock flag on a human's instruction.

        The new scope set replaces every ``context_repo_scopes`` row of the
        record. Give it as ``scope_rows``
        (``[{"scope_type": ..., "scope_value": ...}, ...]``) or as
        ``repo_identifiers`` (each becomes a ``scope_type="repo"`` row;
        ``None``/``[]`` means org-global). ``scope_rows`` wins when both are given.
        ``priority`` is computed from ``scope_type``; duplicate pairs are dropped.

        Locks the row and writes the previous and new ``is_locked`` and scope set
        to ``context_scope_audit_log`` before applying the update.

        Returns the updated row with ``scope_repo_identifiers`` (repo tier only)
        and ``scope_rows`` (full typed set), or ``None`` if the tenant has no such
        record (surface as 404).
        """
        import uuid

        from sqlalchemy import delete, select

        from checkcord.storage.models import CanonicalContext as DBCanonicalContext
        from checkcord.storage.models import ContextRepoScope as DBContextRepoScope
        from checkcord.storage.models import ContextScopeAuditLog as DBContextScopeAuditLog

        if scope_rows is not None:
            normalized_rows = list(
                dict.fromkeys((row["scope_type"], row["scope_value"]) for row in scope_rows)
            )
        else:
            normalized_rows = [("repo", repo) for repo in dict.fromkeys(repo_identifiers or [])]

        normalized_repo_only = [value for (stype, value) in normalized_rows if stype == "repo"]

        lock_stmt = (
            select(DBCanonicalContext.__table__.c.is_locked)
            .where(
                (DBCanonicalContext.__table__.c.id == canonical_id)
                & (DBCanonicalContext.__table__.c.tenant_id == tenant_id)
            )
            .with_for_update()
        )
        update_stmt = (
            update(DBCanonicalContext.__table__)
            .where(
                (DBCanonicalContext.__table__.c.id == canonical_id)
                & (DBCanonicalContext.__table__.c.tenant_id == tenant_id)
            )
            .values(
                is_locked=is_locked,
                updated_at=datetime.now(UTC),
            )
        )
        delete_scopes_stmt = delete(DBContextRepoScope.__table__).where(
            (DBContextRepoScope.__table__.c.canonical_id == canonical_id)
            & (DBContextRepoScope.__table__.c.tenant_id == tenant_id)
        )
        new_scope_rows_json = [
            {"scope_type": stype, "scope_value": value} for (stype, value) in normalized_rows
        ] or None
        audit_row = {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "canonical_id": canonical_id,
            "changed_by": changed_by,
            "new_scope_repo_identifiers": normalized_repo_only or None,
            "new_scope_rows": new_scope_rows_json,
            "new_is_locked": is_locked,
            "changed_at": datetime.now(UTC),
        }

        async def _apply(conn: AsyncConnection) -> dict[str, Any] | None:
            current = (await conn.execute(lock_stmt)).mappings().fetchone()
            if current is None:
                return None
            previous_repo_scopes = await self._fetch_repo_scopes_by_canonical_ids(
                conn, tenant_id, [canonical_id]
            )
            previous_all_scopes = await self._fetch_scope_rows_by_canonical_ids(
                conn, tenant_id, [canonical_id]
            )
            await conn.execute(
                pg_insert(DBContextScopeAuditLog.__table__).values(
                    {
                        **audit_row,
                        "previous_scope_repo_identifiers": (
                            previous_repo_scopes[canonical_id] or None
                        ),
                        "previous_scope_rows": (previous_all_scopes[canonical_id] or None),
                        "previous_is_locked": current["is_locked"],
                    }
                )
            )
            await conn.execute(delete_scopes_stmt)
            if normalized_rows:
                now = datetime.now(UTC)
                await conn.execute(
                    pg_insert(DBContextRepoScope.__table__).values(
                        [
                            {
                                "canonical_id": canonical_id,
                                "scope_type": stype,
                                "scope_value": value,
                                "priority": _priority_for_scope_type(stype),
                                "tenant_id": tenant_id,
                                "created_at": now,
                            }
                            for (stype, value) in normalized_rows
                        ]
                    )
                )
            await conn.execute(update_stmt)
            context_row = await self._fetch_context_row(conn, tenant_id, canonical_id)
            if context_row is None:
                return None
            context_row["scope_repo_identifiers"] = normalized_repo_only
            context_row["scope_rows"] = [
                {"scope_type": stype, "scope_value": value} for (stype, value) in normalized_rows
            ]
            return context_row

        if isinstance(self._bind, AsyncConnection):
            return await _apply(self._bind)
        else:
            async with self._bind.begin() as conn:
                return await _apply(conn)

    async def _fetch_context_row(
        self, conn: AsyncConnection, tenant_id: UUID, canonical_id: UUID
    ) -> dict[str, Any] | None:
        from checkcord.storage.models import CanonicalContext as DBCanonicalContext

        stmt = DBCanonicalContext.__table__.select().where(
            (DBCanonicalContext.__table__.c.id == canonical_id)
            & (DBCanonicalContext.__table__.c.tenant_id == tenant_id)
        )
        row = (await conn.execute(stmt)).mappings().fetchone()
        return dict(row) if row is not None else None

    async def get_latest_scope_change(
        self, tenant_id: UUID, canonical_id: UUID
    ) -> dict[str, Any] | None:
        """Most recent ``context_scope_audit_log`` entry for a context (Task
        G.3) -- powers the scope-edit UI's "last changed by X at Y" display
        so a conflicting concurrent edit is at least visible after the fact.
        Returns ``None`` if the context has never had a scope/is_locked edit.
        """
        from sqlalchemy import select

        from checkcord.storage.models import ContextScopeAuditLog as DBContextScopeAuditLog

        stmt = (
            select(
                DBContextScopeAuditLog.__table__.c.changed_by,
                DBContextScopeAuditLog.__table__.c.changed_at,
            )
            .where(
                (DBContextScopeAuditLog.__table__.c.tenant_id == tenant_id)
                & (DBContextScopeAuditLog.__table__.c.canonical_id == canonical_id)
            )
            .order_by(DBContextScopeAuditLog.__table__.c.changed_at.desc())
            .limit(1)
        )

        async def _fetch(conn: AsyncConnection) -> dict[str, Any] | None:
            result = await conn.execute(stmt)
            row = result.mappings().fetchone()
            if row is None:
                return None
            return {"changed_by": row["changed_by"], "changed_at": row["changed_at"].isoformat()}

        if isinstance(self._bind, AsyncConnection):
            return await _fetch(self._bind)
        else:
            async with self._bind.begin() as conn:
                return await _fetch(conn)

    async def get_canonical_context(
        self,
        tenant_id: UUID,
        canonical_id: UUID,
    ) -> dict[str, Any] | None:
        """Fetch a single canonical context."""
        from checkcord.storage.models import CanonicalContext as DBCanonicalContext

        stmt = DBCanonicalContext.__table__.select().where(
            (DBCanonicalContext.__table__.c.id == canonical_id)
            & (DBCanonicalContext.__table__.c.tenant_id == tenant_id)
        )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            row = result.mappings().fetchone()
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                row = result.mappings().fetchone()

        return dict(row) if row is not None else None

    async def get_event_hold(self, tenant_id: UUID, event_id: UUID) -> tuple[bool, str | None]:
        """Whether the event is stored, and the connection it is held under (None if live)."""
        from sqlalchemy import select

        stmt = select(events_table.c.held_connection).where(
            (events_table.c.tenant_id == tenant_id) & (events_table.c.event_id == event_id)
        )
        if isinstance(self._bind, AsyncConnection):
            row = (await self._bind.execute(stmt)).first()
        else:
            async with self._bind.begin() as conn:
                row = (await conn.execute(stmt)).first()
        return (False, None) if row is None else (True, row.held_connection)

    async def get_record_holds(
        self, tenant_id: UUID, canonical_ids: list[UUID]
    ) -> dict[UUID, str | None]:
        """The ``held_connection`` of each record found, by id."""
        from sqlalchemy import select

        from checkcord.storage.models import CanonicalContext as DBCanonicalContext

        records = DBCanonicalContext.__table__
        stmt = select(records.c.id, records.c.held_connection).where(
            (records.c.tenant_id == tenant_id) & records.c.id.in_(canonical_ids)
        )
        if isinstance(self._bind, AsyncConnection):
            rows = (await self._bind.execute(stmt)).all()
        else:
            async with self._bind.begin() as conn:
                rows = (await conn.execute(stmt)).all()
        return {r.id: r.held_connection for r in rows}

    async def _fetch_sources_by_canonical_ids(
        self,
        conn: AsyncConnection,
        tenant_id: UUID,
        canonical_ids: list[UUID],
    ) -> dict[UUID, list[dict[str, Any]]]:
        """Batch-fetch summarized source citations for a set of canonical contexts.

        Selects citation fields only (event_id, source_platform, source_type,
        happened_at, and ``chat_source`` for chat records), not ``content_clean``.
        Returns an entry, possibly ``[]``,
        for every id passed in.
        """
        from sqlalchemy import select

        from checkcord.storage.models import ContextEventLink as DBContextEventLink

        sources_by_context: dict[UUID, list[dict[str, Any]]] = {cid: [] for cid in canonical_ids}
        if not canonical_ids:
            return sources_by_context

        stmt = (
            select(
                DBContextEventLink.__table__.c.canonical_id,
                events_table.c.event_id,
                events_table.c.source_platform,
                events_table.c.source_type,
                events_table.c.happened_at,
                events_table.c.metadata["chat_source"].label("chat_source"),
            )
            .select_from(
                DBContextEventLink.__table__.join(
                    events_table,
                    DBContextEventLink.__table__.c.event_id == events_table.c.event_id,
                )
            )
            .where(
                (DBContextEventLink.__table__.c.tenant_id == tenant_id)
                & (DBContextEventLink.__table__.c.canonical_id.in_(canonical_ids))
                & (events_table.c.tenant_id == tenant_id)
            )
        )

        result = await conn.execute(stmt)
        for r in result.mappings().fetchall():
            source = {
                "event_id": str(r["event_id"]),
                "source_platform": r["source_platform"],
                "source_type": r["source_type"],
                "happened_at": r["happened_at"].isoformat() if r["happened_at"] else None,
            }
            if r["chat_source"]:
                source["chat_source"] = r["chat_source"]
            sources_by_context[r["canonical_id"]].append(source)
        return sources_by_context

    async def _fetch_repo_scopes_by_canonical_ids(
        self,
        conn: AsyncConnection,
        tenant_id: UUID,
        canonical_ids: list[UUID],
    ) -> dict[UUID, list[str]]:
        """Batch-fetch each record's repo-tier scope set, keyed by canonical_id.

        Team and path rows are left out. Returns an entry, possibly ``[]``, for
        every id passed in.
        """
        from sqlalchemy import select

        from checkcord.storage.models import ContextRepoScope as DBContextRepoScope

        scopes_by_context: dict[UUID, list[str]] = {cid: [] for cid in canonical_ids}
        if not canonical_ids:
            return scopes_by_context

        stmt = select(
            DBContextRepoScope.__table__.c.canonical_id,
            DBContextRepoScope.__table__.c.scope_value,
        ).where(
            (DBContextRepoScope.__table__.c.tenant_id == tenant_id)
            & (DBContextRepoScope.__table__.c.canonical_id.in_(canonical_ids))
            & (DBContextRepoScope.__table__.c.scope_type == "repo")
        )
        result = await conn.execute(stmt)
        for r in result.mappings().fetchall():
            scopes_by_context[r["canonical_id"]].append(r["scope_value"])
        return scopes_by_context

    async def _fetch_scope_rows_by_canonical_ids(
        self,
        conn: AsyncConnection,
        tenant_id: UUID,
        canonical_ids: list[UUID],
    ) -> dict[UUID, list[dict[str, str]]]:
        """Batch-fetch each record's full typed scope set, keyed by canonical_id.

        Values are ``[{"scope_type": ..., "scope_value": ...}, ...]``.
        """
        from sqlalchemy import select

        from checkcord.storage.models import ContextRepoScope as DBContextRepoScope

        scopes_by_context: dict[UUID, list[dict[str, str]]] = {cid: [] for cid in canonical_ids}
        if not canonical_ids:
            return scopes_by_context

        stmt = select(
            DBContextRepoScope.__table__.c.canonical_id,
            DBContextRepoScope.__table__.c.scope_type,
            DBContextRepoScope.__table__.c.scope_value,
        ).where(
            (DBContextRepoScope.__table__.c.tenant_id == tenant_id)
            & (DBContextRepoScope.__table__.c.canonical_id.in_(canonical_ids))
        )
        result = await conn.execute(stmt)
        for r in result.mappings().fetchall():
            scopes_by_context[r["canonical_id"]].append(
                {"scope_type": r["scope_type"], "scope_value": r["scope_value"]}
            )
        return scopes_by_context

    async def get_context_scope_rows(
        self, tenant_id: UUID, canonical_id: UUID
    ) -> list[dict[str, str]]:
        """Full typed scope set of one record.

        Empty list means org-global; one or more entries means scoped to
        exactly those ``(scope_type, scope_value)`` pairs.
        """

        async def _fetch(conn: AsyncConnection) -> list[dict[str, str]]:
            scopes = await self._fetch_scope_rows_by_canonical_ids(conn, tenant_id, [canonical_id])
            return scopes[canonical_id]

        if isinstance(self._bind, AsyncConnection):
            return await _fetch(self._bind)
        else:
            async with self._bind.begin() as conn:
                return await _fetch(conn)

    async def get_org_global_ids(
        self, tenant_id: UUID, canonical_ids: list[UUID | str]
    ) -> set[UUID]:
        """Which of these canonical ids are org-global (no ``context_repo_scopes`` rows).

        Accepts ``UUID`` or ``str`` elements and normalises to ``UUID`` before
        comparing; search results carry string ids, and comparing those with
        UUIDs would classify every scoped record as org-global.
        """
        from sqlalchemy import select

        from checkcord.storage.models import ContextRepoScope as DBContextRepoScope

        normalized_ids = {cid if isinstance(cid, UUID) else UUID(cid) for cid in canonical_ids}

        async def _fetch(conn: AsyncConnection) -> set[UUID]:
            if not normalized_ids:
                return set()
            stmt = select(DBContextRepoScope.__table__.c.canonical_id.distinct()).where(
                (DBContextRepoScope.__table__.c.tenant_id == tenant_id)
                & (DBContextRepoScope.__table__.c.canonical_id.in_(normalized_ids))
            )
            result = await conn.execute(stmt)
            scoped_ids = {row[0] for row in result.fetchall()}
            return normalized_ids - scoped_ids

        if isinstance(self._bind, AsyncConnection):
            return await _fetch(self._bind)
        else:
            async with self._bind.begin() as conn:
                return await _fetch(conn)

    async def record_user_repo_query(
        self, tenant_id: UUID, user_id: UUID, repo_identifier: str
    ) -> None:
        """Increment the usage counter for ``(tenant, user, repo)``.

        Called only from the MCP search path with a resolved ``user_id``; the
        caller excludes ``ANONYMOUS_USER_ID``.
        """
        from checkcord.storage.models import UserRepoAffinity as DBUserRepoAffinity

        now = datetime.now(UTC)
        stmt = (
            pg_insert(DBUserRepoAffinity.__table__)
            .values(
                id=uuid4(),
                tenant_id=tenant_id,
                user_id=user_id,
                repo_identifier=repo_identifier,
                query_count=1,
                last_queried_at=now,
            )
            .on_conflict_do_update(
                index_elements=["tenant_id", "user_id", "repo_identifier"],
                set_={
                    "query_count": DBUserRepoAffinity.__table__.c.query_count + 1,
                    "last_queried_at": now,
                },
            )
        )

        if isinstance(self._bind, AsyncConnection):
            await self._bind.execute(stmt)
        else:
            async with self._bind.begin() as conn:
                await conn.execute(stmt)

    async def get_user_repo_affinity(
        self, tenant_id: UUID, user_id: UUID, repo_identifier: str
    ) -> int:
        """Current ``query_count`` for ``(tenant, user, repo)``, or 0."""
        from sqlalchemy import select

        from checkcord.storage.models import UserRepoAffinity as DBUserRepoAffinity

        stmt = select(DBUserRepoAffinity.__table__.c.query_count).where(
            (DBUserRepoAffinity.__table__.c.tenant_id == tenant_id)
            & (DBUserRepoAffinity.__table__.c.user_id == user_id)
            & (DBUserRepoAffinity.__table__.c.repo_identifier == repo_identifier)
        )

        async def _fetch(conn: AsyncConnection) -> int:
            result = await conn.execute(stmt)
            row = result.fetchone()
            return int(row[0]) if row else 0

        if isinstance(self._bind, AsyncConnection):
            return await _fetch(self._bind)
        else:
            async with self._bind.begin() as conn:
                return await _fetch(conn)

    async def find_similar_contexts(
        self,
        tenant_id: UUID,
        embedding: list[float],
        threshold: float = 0.85,
        repo_identifier: str | None = None,
        include_sources: bool = True,
        query_text: str | None = None,
        limit: int | None = None,
        team_identifier: str | None = None,
        path: str | None = None,
        held_connection: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find canonical contexts similar to the given embedding above the threshold.

        Only records with this ``held_connection`` match: live records by default,
        or the records held under one connection.

        A match is eligible only if it is org-global or scoped to the given
        ``repo_identifier``, ``team_identifier`` or ``path`` (see
        ``_scope_filter``). With none given, only org-global records match.

        Each match carries ``content_hash``, ``last_verified_at`` and, unless
        ``include_sources`` is False, a summarized ``sources`` list.

        ``query_text`` adds a full-text candidate query (``plainto_tsquery`` on
        ``summary_tsv``) fused with the vector candidates by Reciprocal Rank
        Fusion (see ``_fuse_rrf``). Lexical candidates are not gated by
        ``threshold``, so an exact identifier or error code can surface when its
        embedding similarity is low.

        ``limit`` is a SQL ``LIMIT`` on the vector candidate query. With
        ``query_text`` the fused list is also cut to ``limit``, because the
        lexical branch has its own cap of 50.
        """
        from sqlalchemy import func, select

        from checkcord.storage.models import CanonicalContext as DBCanonicalContext

        distance = DBCanonicalContext.embedding.cosine_distance(embedding)
        scope_filter = (
            (DBCanonicalContext.tenant_id == tenant_id)
            & DBCanonicalContext.held_connection.is_not_distinct_from(held_connection)
            & _scope_filter(
                tenant_id,
                repo_identifier=repo_identifier,
                team_identifier=team_identifier,
                path=path,
            )
        )

        vector_stmt = (
            select(
                DBCanonicalContext.id,
                DBCanonicalContext.summary,
                DBCanonicalContext.summary_redacted,
                DBCanonicalContext.confidence_score,
                DBCanonicalContext.content_hash,
                DBCanonicalContext.last_verified_at,
                DBCanonicalContext.context_type,
                DBCanonicalContext.severity,
                (1.0 - distance).label("similarity"),
            )
            .where(scope_filter & (distance < (1.0 - threshold)))
            .order_by(distance)
        )
        if limit is not None:
            vector_stmt = vector_stmt.limit(limit)

        lexical_stmt = None
        stripped_query_text = query_text.strip() if query_text else ""
        if stripped_query_text:
            tsquery = func.plainto_tsquery("english", stripped_query_text)
            rank = func.ts_rank(DBCanonicalContext.summary_tsv, tsquery)
            lexical_stmt = (
                select(
                    DBCanonicalContext.id,
                    DBCanonicalContext.summary,
                    DBCanonicalContext.summary_redacted,
                    DBCanonicalContext.confidence_score,
                    DBCanonicalContext.content_hash,
                    DBCanonicalContext.last_verified_at,
                    DBCanonicalContext.context_type,
                    DBCanonicalContext.severity,
                    (1.0 - distance).label("similarity"),
                )
                .where(scope_filter & DBCanonicalContext.summary_tsv.op("@@")(tsquery))
                .order_by(rank.desc())
                .limit(50)
            )

        async def _fetch(conn: AsyncConnection) -> list[dict[str, Any]]:
            vector_result = await conn.execute(vector_stmt)
            rows = [dict(r) for r in vector_result.mappings().fetchall()]

            if lexical_stmt is not None:
                lexical_result = await conn.execute(lexical_stmt)
                lexical_rows = [dict(r) for r in lexical_result.mappings().fetchall()]
                rows = _fuse_rrf(rows, lexical_rows)
                if limit is not None:
                    rows = rows[:limit]

            sources_by_id: dict[UUID, list[dict[str, Any]]] = {}
            if include_sources:
                sources_by_id = await self._fetch_sources_by_canonical_ids(
                    conn, tenant_id, [row["id"] for row in rows]
                )
            for row in rows:
                if include_sources:
                    row["sources"] = sources_by_id.get(row["id"], [])
                row["last_verified_at"] = (
                    row["last_verified_at"].isoformat() if row["last_verified_at"] else None
                )
                row["id"] = str(row["id"])
            return rows

        if isinstance(self._bind, AsyncConnection):
            return await _fetch(self._bind)
        else:
            async with self._bind.begin() as conn:
                return await _fetch(conn)

    async def get_events_for_context(
        self,
        tenant_id: UUID,
        canonical_id: UUID,
    ) -> list[UUID]:
        """Fetch all event IDs linked to a canonical context."""
        from checkcord.storage.models import ContextEventLink as DBContextEventLink

        stmt = DBContextEventLink.__table__.select().where(
            (DBContextEventLink.__table__.c.canonical_id == canonical_id)
            & (DBContextEventLink.__table__.c.tenant_id == tenant_id)
        )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            rows = result.mappings().fetchall()
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                rows = result.mappings().fetchall()

        return [r["event_id"] for r in rows]

    async def get_stale_contexts_global(self, since: datetime) -> list[tuple[UUID, UUID]]:
        """Fetch unique context IDs and their tenant IDs that have new event links since `since`."""
        from sqlalchemy import select

        from checkcord.storage.models import ContextEventLink as DBContextEventLink

        stmt = (
            select(DBContextEventLink.tenant_id, DBContextEventLink.canonical_id)
            .where(DBContextEventLink.created_at >= since)
            .distinct()
        )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            rows = result.fetchall()
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                rows = result.fetchall()

        return [(row[0], row[1]) for row in rows]

    async def update_context_scores_batch_global(
        self, updates: list[tuple[UUID, UUID, float]]
    ) -> int:
        """Update confidence scores for multiple contexts globally.

        ``updates`` is a list of (tenant_id, context_id, new_score). Also sets
        ``last_verified_at`` to now for each; callers include a record only when
        its confidence was recomputed.
        """
        if not updates:
            return 0

        from sqlalchemy import bindparam, update

        from checkcord.storage.models import CanonicalContext as DBCanonicalContext

        now = datetime.now(UTC)
        stmt = (
            update(DBCanonicalContext.__table__)
            .where(DBCanonicalContext.__table__.c.id == bindparam("b_context_id"))
            .where(DBCanonicalContext.__table__.c.tenant_id == bindparam("b_tenant_id"))
            .values(
                confidence_score=bindparam("b_new_score"),
                updated_at=now,
                last_verified_at=now,
            )
        )
        values_list = [
            {"b_tenant_id": t_id, "b_context_id": c_id, "b_new_score": score}
            for t_id, c_id, score in updates
        ]

        if isinstance(self._bind, AsyncConnection):
            res = await self._bind.execute(stmt, values_list)
            return res.rowcount or 0
        else:
            async with self._bind.begin() as conn:
                res = await conn.execute(stmt, values_list)
                return res.rowcount or 0

    async def get_canonical_contexts_missing_embedding(
        self, after_id: UUID | None, limit: int
    ) -> list[tuple[UUID, UUID, str]]:
        """Page through canonical_contexts with a NULL embedding, across all tenants.

        Used by scripts/reembed_canonical_contexts.py. The cursor is
        ``id > after_id``, not OFFSET: OFFSET would skip rows as earlier batches
        leave the filter, and a row that never embeds (an empty summary) would
        loop forever without a cursor that moves past it.
        """
        from sqlalchemy import select

        from checkcord.storage.models import CanonicalContext as DBCanonicalContext

        stmt = (
            select(
                DBCanonicalContext.id,
                DBCanonicalContext.tenant_id,
                DBCanonicalContext.summary,
            )
            .where(DBCanonicalContext.embedding.is_(None))
            .order_by(DBCanonicalContext.id)
            .limit(limit)
        )
        if after_id is not None:
            stmt = stmt.where(DBCanonicalContext.id > after_id)

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            rows = result.fetchall()
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                rows = result.fetchall()

        return [(row[0], row[1], row[2]) for row in rows]

    async def update_canonical_context_embeddings_batch(
        self, updates: list[tuple[UUID, UUID, list[float]]]
    ) -> int:
        """Write re-embedded vectors for multiple contexts globally, in one batched UPDATE.

        ``updates`` is a list of (tenant_id, context_id, embedding) tuples --
        same shape convention as update_context_scores_batch_global. Rows
        whose re-embed produced None (empty/whitespace-only summary) must not
        be included by the caller; this method has no "clear it back to NULL"
        branch because there is nothing to write for those rows -- they stay
        NULL, which is already their current value.
        """
        if not updates:
            return 0

        from sqlalchemy import bindparam, update

        from checkcord.storage.models import CanonicalContext as DBCanonicalContext

        stmt = (
            update(DBCanonicalContext.__table__)
            .where(DBCanonicalContext.__table__.c.id == bindparam("b_context_id"))
            .where(DBCanonicalContext.__table__.c.tenant_id == bindparam("b_tenant_id"))
            .values(embedding=bindparam("b_embedding"))
        )
        values_list = [
            {"b_tenant_id": t_id, "b_context_id": c_id, "b_embedding": embedding}
            for t_id, c_id, embedding in updates
        ]

        if isinstance(self._bind, AsyncConnection):
            res = await self._bind.execute(stmt, values_list)
            return res.rowcount or 0
        else:
            async with self._bind.begin() as conn:
                res = await conn.execute(stmt, values_list)
                return res.rowcount or 0

    async def get_events_by_ids(
        self,
        tenant_id: UUID,
        event_ids: list[UUID],
    ) -> list[CanonicalEvent]:
        """Fetch multiple events by their IDs within a tenant."""
        if not event_ids:
            return []

        stmt = events_table.select().where(
            (events_table.c.event_id.in_(event_ids)) & (events_table.c.tenant_id == tenant_id)
        )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            rows = result.mappings().fetchall()
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                rows = result.mappings().fetchall()

        return [self._row_to_event(dict(row)) for row in rows]

    async def find_ast_nodes_by_symbols(
        self,
        tenant_id: UUID,
        symbols: list[str],
    ) -> list[dict[str, Any]]:
        """Find AST nodes by exact symbol name within a tenant."""
        if not symbols:
            return []

        from checkcord.storage.models import ASTNode as DBASTNode

        stmt = DBASTNode.__table__.select().where(
            (DBASTNode.__table__.c.tenant_id == tenant_id)
            & (DBASTNode.__table__.c.symbol_name.in_(symbols))
        )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            rows = result.mappings().fetchall()
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                rows = result.mappings().fetchall()

        return [dict(r) for r in rows]

    async def link_ast_to_context(
        self,
        tenant_id: UUID,
        ast_node_id: UUID,
        canonical_id: UUID,
    ) -> bool:
        """Link an AST node to a canonical context in the junction table."""
        from checkcord.models.enums import ASTEdgeStatus
        from checkcord.storage.models import ASTContextEdge as DBASTContextEdge

        row = {
            "ast_node_id": ast_node_id,
            "canonical_id": canonical_id,
            "tenant_id": tenant_id,
            "status": ASTEdgeStatus.ACTIVE,
            "last_accessed": datetime.now(UTC),
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        stmt = (
            pg_insert(DBASTContextEdge.__table__)
            .values(row)
            .on_conflict_do_nothing(
                index_elements=["ast_node_id", "canonical_id"],
            )
        )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            inserted = (result.rowcount or 0) > 0
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                inserted = (result.rowcount or 0) > 0

        return inserted

    async def flag_stale_edges(
        self,
        tenant_id: UUID,
        repo_identifier: str,
        cutoff_date: datetime,
    ) -> int:
        """Mark AST context edges as stale if they haven't been accessed before cutoff_date."""
        from sqlalchemy import select, update

        from checkcord.models.enums import ASTEdgeStatus
        from checkcord.storage.models import ASTContextEdge as DBASTContextEdge
        from checkcord.storage.models import ASTNode as DBASTNode
        from checkcord.storage.models import CanonicalContext as DBCanonicalContext

        # First, select the matching edges to update
        select_stmt = (
            select(
                DBASTContextEdge.__table__.c.ast_node_id,
                DBASTContextEdge.__table__.c.canonical_id,
            )
            .select_from(
                DBASTContextEdge.__table__.join(
                    DBASTNode.__table__,
                    DBASTNode.__table__.c.id == DBASTContextEdge.__table__.c.ast_node_id,
                ).join(
                    DBCanonicalContext.__table__,
                    DBCanonicalContext.__table__.c.id == DBASTContextEdge.__table__.c.canonical_id,
                )
            )
            .where(
                (DBASTContextEdge.__table__.c.tenant_id == tenant_id)
                & (DBASTNode.__table__.c.repo_identifier == repo_identifier)
                & (DBASTContextEdge.__table__.c.status == ASTEdgeStatus.ACTIVE.value)
                & (DBASTContextEdge.__table__.c.last_accessed < cutoff_date)
                & (DBCanonicalContext.__table__.c.is_locked.is_(False))
            )
        )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(select_stmt)
            rows = result.mappings().fetchall()
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(select_stmt)
                rows = result.mappings().fetchall()

        if not rows:
            return 0

        # Now, update them
        from sqlalchemy import and_, or_

        conds = [
            and_(
                DBASTContextEdge.__table__.c.ast_node_id == r["ast_node_id"],
                DBASTContextEdge.__table__.c.canonical_id == r["canonical_id"],
            )
            for r in rows
        ]

        chunk_size = 500
        updated_count = 0
        for i in range(0, len(conds), chunk_size):
            batch_conds = conds[i : i + chunk_size]
            update_stmt = (
                update(DBASTContextEdge.__table__)
                .where((DBASTContextEdge.__table__.c.tenant_id == tenant_id) & or_(*batch_conds))
                .values(status=ASTEdgeStatus.STALE.value, updated_at=datetime.now(UTC))
            )

            if isinstance(self._bind, AsyncConnection):
                res = await self._bind.execute(update_stmt)
                updated_count += res.rowcount or 0
            else:
                async with self._bind.begin() as conn:
                    res = await conn.execute(update_stmt)
                    updated_count += res.rowcount or 0

        return updated_count

    async def get_stale_edges_for_repo(
        self,
        tenant_id: UUID,
        repo_identifier: str,
    ) -> list[dict[str, Any]]:
        """Fetch AST context edges that are currently marked as STALE for a repository.
        Returns a list of dicts with node and context details.
        """
        from sqlalchemy import select

        from checkcord.models.enums import ASTEdgeStatus
        from checkcord.storage.models import ASTContextEdge as DBASTContextEdge
        from checkcord.storage.models import ASTNode as DBASTNode
        from checkcord.storage.models import CanonicalContext as DBCanonicalContext

        stmt = (
            select(
                DBASTNode.__table__.c.symbol_name,
                DBASTNode.__table__.c.file_path,
                DBASTNode.__table__.c.id.label("ast_node_id"),
                DBCanonicalContext.__table__.c.id.label("canonical_id"),
                DBCanonicalContext.__table__.c.summary,
                DBCanonicalContext.__table__.c.summary_redacted,
                DBCanonicalContext.__table__.c.access_control,
            )
            .select_from(
                DBASTContextEdge.__table__.join(
                    DBASTNode.__table__,
                    DBASTNode.__table__.c.id == DBASTContextEdge.__table__.c.ast_node_id,
                ).join(
                    DBCanonicalContext.__table__,
                    DBCanonicalContext.__table__.c.id == DBASTContextEdge.__table__.c.canonical_id,
                )
            )
            .where(
                (DBASTContextEdge.__table__.c.tenant_id == tenant_id)
                & (DBASTNode.__table__.c.repo_identifier == repo_identifier)
                & (DBASTContextEdge.__table__.c.status == ASTEdgeStatus.STALE.value)
            )
        )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            rows = result.mappings().fetchall()
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                rows = result.mappings().fetchall()

        return [dict(r) for r in rows]

    async def mark_edges_deprecated(
        self,
        tenant_id: UUID,
        edge_keys: list[tuple[UUID, UUID]],
    ) -> int:
        """Mark specific AST context edges as DEPRECATED.
        edge_keys is a list of tuples: (ast_node_id, canonical_id).
        """
        if not edge_keys:
            return 0

        from sqlalchemy import and_, or_, update

        from checkcord.models.enums import ASTEdgeStatus
        from checkcord.storage.models import ASTContextEdge as DBASTContextEdge

        conds = [
            and_(
                DBASTContextEdge.__table__.c.ast_node_id == node_id,
                DBASTContextEdge.__table__.c.canonical_id == canonical_id,
            )
            for node_id, canonical_id in edge_keys
        ]

        chunk_size = 500
        updated_count = 0
        for i in range(0, len(conds), chunk_size):
            batch_conds = conds[i : i + chunk_size]
            update_stmt = (
                update(DBASTContextEdge.__table__)
                .where((DBASTContextEdge.__table__.c.tenant_id == tenant_id) & or_(*batch_conds))
                .values(status=ASTEdgeStatus.DEPRECATED.value, updated_at=datetime.now(UTC))
            )

            if isinstance(self._bind, AsyncConnection):
                res = await self._bind.execute(update_stmt)
                updated_count += res.rowcount or 0
            else:
                async with self._bind.begin() as conn:
                    res = await conn.execute(update_stmt)
                    updated_count += res.rowcount or 0

        return updated_count

    async def upsert_ast_node_and_check_drift(
        self,
        tenant_id: UUID,
        repo_identifier: str,
        file_path: str,
        symbol_name: str,
        node_type: str,
        merkle_hash: str,
    ) -> bool:
        """Upsert an AST node. If it exists and the merkle_hash has changed,
        detect drift, set all connected context edges to 'stale', and update the hash.
        Returns True if drift was detected, False otherwise.
        """
        from sqlalchemy import insert, select, update

        from checkcord.models.enums import ASTEdgeStatus
        from checkcord.storage.models import ASTContextEdge as DBASTContextEdge
        from checkcord.storage.models import ASTNode as DBASTNode

        file_path = normalize_file_path(file_path, repo_identifier)

        # Check for existing node
        select_stmt = select(
            DBASTNode.__table__.c.id,
            DBASTNode.__table__.c.merkle_hash,
        ).where(
            (DBASTNode.__table__.c.tenant_id == tenant_id)
            & (DBASTNode.__table__.c.repo_identifier == repo_identifier)
            & (DBASTNode.__table__.c.file_path == file_path)
            & (DBASTNode.__table__.c.symbol_name == symbol_name)
            & (DBASTNode.__table__.c.node_type == node_type)
        )

        if isinstance(self._bind, AsyncConnection):
            res = await self._bind.execute(select_stmt)
            existing = res.mappings().fetchone()
        else:
            async with self._bind.begin() as conn:
                res = await conn.execute(select_stmt)
                existing = res.mappings().fetchone()

        drift_detected = False

        if existing is not None:
            node_id = existing["id"]
            old_hash = existing["merkle_hash"]
            if old_hash != merkle_hash:
                drift_detected = True
                logger.info(
                    "drift_detector.drift_detected",
                    tenant_id=str(tenant_id),
                    repo_identifier=repo_identifier,
                    file_path=file_path,
                    symbol_name=symbol_name,
                    node_type=node_type,
                    old_hash=old_hash,
                    new_hash=merkle_hash,
                )
                # Update merkle_hash
                update_node_stmt = (
                    update(DBASTNode.__table__)
                    .where(DBASTNode.__table__.c.id == node_id)
                    .values(merkle_hash=merkle_hash, updated_at=datetime.now(UTC))
                )

                # Mark edges as stale
                update_edges_stmt = (
                    update(DBASTContextEdge.__table__)
                    .where(
                        (DBASTContextEdge.__table__.c.ast_node_id == node_id)
                        & (DBASTContextEdge.__table__.c.tenant_id == tenant_id)
                    )
                    .values(
                        status=ASTEdgeStatus.STALE.value,
                        updated_at=datetime.now(UTC),
                    )
                )

                if isinstance(self._bind, AsyncConnection):
                    await self._bind.execute(update_node_stmt)
                    await self._bind.execute(update_edges_stmt)
                else:
                    async with self._bind.begin() as conn:
                        await conn.execute(update_node_stmt)
                        await conn.execute(update_edges_stmt)
        else:
            # Insert new AST Node
            import uuid

            node_id = uuid.uuid4()
            insert_stmt = insert(DBASTNode.__table__).values(
                id=node_id,
                tenant_id=tenant_id,
                repo_identifier=repo_identifier,
                file_path=file_path,
                symbol_name=symbol_name,
                node_type=node_type,
                merkle_hash=merkle_hash,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            if isinstance(self._bind, AsyncConnection):
                await self._bind.execute(insert_stmt)
            else:
                async with self._bind.begin() as conn:
                    await conn.execute(insert_stmt)

        return drift_detected

    async def get_context_for_symbol(
        self,
        tenant_id: UUID,
        symbol: str,
        file_path: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch canonical contexts linked to an active AST node with the given symbol name."""
        from sqlalchemy import or_, select

        from checkcord.models.enums import ASTEdgeStatus
        from checkcord.storage.models import ASTContextEdge as DBASTContextEdge
        from checkcord.storage.models import ASTNode as DBASTNode
        from checkcord.storage.models import CanonicalContext as DBCanonicalContext

        stmt = (
            select(
                DBCanonicalContext.__table__.c.id,
                DBCanonicalContext.__table__.c.summary,
                DBCanonicalContext.__table__.c.summary_redacted,
                DBCanonicalContext.__table__.c.confidence_score,
            )
            .select_from(
                DBCanonicalContext.__table__.join(
                    DBASTContextEdge.__table__,
                    DBASTContextEdge.__table__.c.canonical_id == DBCanonicalContext.__table__.c.id,
                ).join(
                    DBASTNode.__table__,
                    DBASTNode.__table__.c.id == DBASTContextEdge.__table__.c.ast_node_id,
                )
            )
            .where(
                (DBASTNode.__table__.c.tenant_id == tenant_id)
                & (DBASTContextEdge.__table__.c.tenant_id == tenant_id)
                & (DBCanonicalContext.__table__.c.tenant_id == tenant_id)
                & live_records(DBCanonicalContext.__table__)
                & (DBASTNode.__table__.c.symbol_name == symbol)
                & (DBASTContextEdge.__table__.c.status == ASTEdgeStatus.ACTIVE.value)
            )
        )

        if file_path:
            normalized_path = file_path.replace("\\", "/")
            while normalized_path.startswith("./"):
                normalized_path = normalized_path[2:]

            is_absolute = normalized_path.startswith("/") or (
                len(normalized_path) > 1 and normalized_path[1] == ":"
            )
            if is_absolute:
                stmt = stmt.where(
                    or_(
                        DBASTNode.__table__.c.file_path == normalized_path,
                        text(
                            "RIGHT(:norm_path, LENGTH(ast_nodes.file_path) + 1) = "
                            "'/' || ast_nodes.file_path"
                        ),
                    )
                ).params(norm_path=normalized_path)
            else:
                stmt = stmt.where(DBASTNode.__table__.c.file_path == normalized_path)

        stmt = stmt.order_by(DBCanonicalContext.__table__.c.confidence_score.desc())

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            rows = result.mappings().fetchall()
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                rows = result.mappings().fetchall()

        return [dict(row) for row in rows]

    async def get_top_contexts_for_repo(
        self,
        tenant_id: UUID,
        repo_identifier: str,
        limit: int = 10,
        team_identifier: str | None = None,
        path: str | None = None,
        decisions_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch the top canonical contexts in scope for a repo: blocking first, then by
        confidence, then newest. ``decisions_only`` keeps decision records and blocking ones.

        Two independent filters apply:

        1. Governance scope (see ``_scope_filter``): org-global records plus
           records scoped to the given repo, team or path. Never another repo's,
           team's or path's records.
        2. Code drift: if a record is linked to code in ``repo_identifier`` through
           ``ast_context_edges``, at least one such edge must be ``ACTIVE``. A
           record with no edges into this repo is unaffected. The check is per
           repo, so a record scoped to several repos is judged for each one
           separately.
        """
        from sqlalchemy import exists, select

        from checkcord.models.enums import ASTEdgeStatus
        from checkcord.storage.models import ASTContextEdge as DBASTContextEdge
        from checkcord.storage.models import ASTNode as DBASTNode
        from checkcord.storage.models import CanonicalContext as DBCanonicalContext

        edge_into_repo = (
            select(DBASTContextEdge.__table__.c.status)
            .select_from(
                DBASTContextEdge.__table__.join(
                    DBASTNode.__table__,
                    DBASTNode.__table__.c.id == DBASTContextEdge.__table__.c.ast_node_id,
                )
            )
            .where(
                (DBASTContextEdge.__table__.c.canonical_id == DBCanonicalContext.__table__.c.id)
                & (DBASTContextEdge.__table__.c.tenant_id == tenant_id)
                & (DBASTNode.__table__.c.tenant_id == tenant_id)
                & (DBASTNode.__table__.c.repo_identifier == repo_identifier)
            )
        )
        has_no_edge_into_repo = ~exists(edge_into_repo)
        has_active_edge_into_repo = exists(
            edge_into_repo.where(DBASTContextEdge.__table__.c.status == ASTEdgeStatus.ACTIVE.value)
        )

        stmt = (
            DBCanonicalContext.__table__.select()
            .with_only_columns(
                DBCanonicalContext.__table__.c.id,
                DBCanonicalContext.__table__.c.summary,
                DBCanonicalContext.__table__.c.summary_redacted,
                DBCanonicalContext.__table__.c.confidence_score,
                DBCanonicalContext.__table__.c.content_hash,
                DBCanonicalContext.__table__.c.last_verified_at,
                DBCanonicalContext.__table__.c.context_type,
                DBCanonicalContext.__table__.c.severity,
            )
            .where(
                (DBCanonicalContext.__table__.c.tenant_id == tenant_id)
                & _scope_filter(
                    tenant_id,
                    repo_identifier=repo_identifier,
                    team_identifier=team_identifier,
                    path=path,
                )
                & (has_no_edge_into_repo | has_active_edge_into_repo)
            )
            .order_by(
                (DBCanonicalContext.__table__.c.severity == BLOCKING).desc(),
                DBCanonicalContext.__table__.c.confidence_score.desc(),
                DBCanonicalContext.__table__.c.created_at.desc(),
            )
            .limit(limit)
        )
        if decisions_only:
            stmt = stmt.where(decisions_or_blocking(DBCanonicalContext.__table__))

        async def _fetch(conn: AsyncConnection) -> list[dict[str, Any]]:
            result = await conn.execute(stmt)
            rows = [dict(row) for row in result.mappings().fetchall()]
            sources_by_id = await self._fetch_sources_by_canonical_ids(
                conn, tenant_id, [row["id"] for row in rows]
            )
            for row in rows:
                row["sources"] = sources_by_id.get(row["id"], [])
                row["last_verified_at"] = (
                    row["last_verified_at"].isoformat() if row["last_verified_at"] else None
                )
                row["id"] = str(row["id"])
            return rows

        if isinstance(self._bind, AsyncConnection):
            return await _fetch(self._bind)
        else:
            async with self._bind.begin() as conn:
                return await _fetch(conn)

    async def get_org_global_top_contexts(
        self,
        tenant_id: UUID,
        limit: int = 10,
        team_identifier: str | None = None,
        path: str | None = None,
        decisions_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch the top canonical contexts eligible without a specific repo, in the same order
        as ``get_top_contexts_for_repo``.

        With no ``team_identifier`` or ``path``, only org-global records are
        eligible. Passing either also admits records scoped to that team or
        path. No code-drift filter applies, since drift is relative to a repo.
        """
        from sqlalchemy import select

        from checkcord.storage.models import CanonicalContext as DBCanonicalContext

        stmt = (
            select(
                DBCanonicalContext.__table__.c.id,
                DBCanonicalContext.__table__.c.summary,
                DBCanonicalContext.__table__.c.summary_redacted,
                DBCanonicalContext.__table__.c.confidence_score,
                DBCanonicalContext.__table__.c.content_hash,
                DBCanonicalContext.__table__.c.last_verified_at,
                DBCanonicalContext.__table__.c.context_type,
                DBCanonicalContext.__table__.c.severity,
            )
            .where(
                (DBCanonicalContext.__table__.c.tenant_id == tenant_id)
                & _scope_filter(
                    tenant_id, repo_identifier=None, team_identifier=team_identifier, path=path
                )
            )
            .order_by(
                (DBCanonicalContext.__table__.c.severity == BLOCKING).desc(),
                DBCanonicalContext.__table__.c.confidence_score.desc(),
                DBCanonicalContext.__table__.c.created_at.desc(),
            )
            .limit(limit)
        )
        if decisions_only:
            stmt = stmt.where(decisions_or_blocking(DBCanonicalContext.__table__))

        async def _fetch(conn: AsyncConnection) -> list[dict[str, Any]]:
            result = await conn.execute(stmt)
            rows = [dict(row) for row in result.mappings().fetchall()]
            sources_by_id = await self._fetch_sources_by_canonical_ids(
                conn, tenant_id, [row["id"] for row in rows]
            )
            for row in rows:
                row["sources"] = sources_by_id.get(row["id"], [])
                row["last_verified_at"] = (
                    row["last_verified_at"].isoformat() if row["last_verified_at"] else None
                )
                row["id"] = str(row["id"])
            return rows

        if isinstance(self._bind, AsyncConnection):
            return await _fetch(self._bind)
        else:
            async with self._bind.begin() as conn:
                return await _fetch(conn)

    async def find_ast_nodes_by_paths(
        self,
        tenant_id: UUID,
        repo_identifier: str,
        file_paths: list[str],
    ) -> list[dict[str, Any]]:
        """Find all AST nodes and their active context edges for the given files in a repository."""
        if not file_paths:
            return []

        from sqlalchemy import select

        from checkcord.models.enums import ASTEdgeStatus
        from checkcord.storage.models import ASTContextEdge as DBASTContextEdge
        from checkcord.storage.models import ASTNode as DBASTNode
        from checkcord.storage.models import CanonicalContext as DBCanonicalContext

        normalized_paths = [normalize_file_path(p, repo_identifier) for p in file_paths]

        stmt = (
            select(
                DBASTNode.__table__.c.id.label("ast_node_id"),
                DBASTNode.__table__.c.file_path,
                DBASTNode.__table__.c.symbol_name,
                DBASTNode.__table__.c.node_type,
                DBASTNode.__table__.c.merkle_hash,
                DBASTContextEdge.__table__.c.canonical_id,
            )
            .select_from(
                DBASTNode.__table__.outerjoin(
                    DBASTContextEdge.__table__,
                    (DBASTContextEdge.__table__.c.ast_node_id == DBASTNode.__table__.c.id)
                    & (DBASTContextEdge.__table__.c.status == ASTEdgeStatus.ACTIVE.value)
                    & (DBASTContextEdge.__table__.c.tenant_id == tenant_id)
                    & DBASTContextEdge.__table__.c.canonical_id.in_(
                        select(DBCanonicalContext.__table__.c.id).where(
                            (DBCanonicalContext.__table__.c.tenant_id == tenant_id)
                            & live_records(DBCanonicalContext.__table__)
                        )
                    ),
                )
            )
            .where(
                (DBASTNode.__table__.c.tenant_id == tenant_id)
                & (DBASTNode.__table__.c.repo_identifier == repo_identifier)
                & (DBASTNode.__table__.c.file_path.in_(normalized_paths))
            )
        )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            rows = result.mappings().fetchall()
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                rows = result.mappings().fetchall()

        # Touch active context edges to update last_accessed
        active_edges = [
            (row["ast_node_id"], row["canonical_id"])
            for row in rows
            if row["canonical_id"] is not None
        ]
        if active_edges:
            from sqlalchemy import and_, or_

            conds = [
                and_(
                    DBASTContextEdge.__table__.c.ast_node_id == node_id,
                    DBASTContextEdge.__table__.c.canonical_id == canonical_id,
                )
                for node_id, canonical_id in active_edges
            ]
            stmt_update = (
                update(DBASTContextEdge.__table__)
                .where((DBASTContextEdge.__table__.c.tenant_id == tenant_id) & or_(*conds))
                .values(last_accessed=datetime.now(UTC))
            )
            if isinstance(self._bind, AsyncConnection):
                await self._bind.execute(stmt_update)
            else:
                async with self._bind.begin() as conn:
                    await conn.execute(stmt_update)

        return [dict(r) for r in rows]

    # -- private helpers ---------------------------------------------------

    @staticmethod
    def _to_insert_dict(event: CanonicalEvent) -> dict[str, Any]:
        """Convert a *CanonicalEvent* into a flat dict for INSERT.

        * Extracts ``allowed_identity_ids`` from ``access_control`` →
          top-level ``access_allowed_ids`` column.
        * Extracts ``files_changed`` from ``code_metadata`` →
          top-level ``code_files_changed`` column.
        """
        data: dict[str, Any] = event.to_postgres_dict()

        # --- access_control → access_allowed_ids --------------------------
        ac: dict[str, Any] = data["access_control"]
        data["access_allowed_ids"] = ac.pop("allowed_identity_ids", [])

        # --- code_metadata → code_files_changed ---------------------------
        cm: dict[str, Any] | None = data.get("code_metadata")
        if cm is not None:
            data["code_files_changed"] = cm.pop("files_changed", [])
        else:
            data["code_files_changed"] = []

        # StrEnum values → plain strings for non-JSONB columns
        for str_field in (
            "source_platform",
            "source_type",
            "event_action",
            "signal_status",
        ):
            val = data.get(str_field)
            if val is not None and hasattr(val, "value"):
                data[str_field] = val.value

        return data

    @staticmethod
    def _row_to_event(row: dict[str, Any]) -> CanonicalEvent:
        """Reconstruct a *CanonicalEvent* from a database row.

        Merges the top-level array columns back into their parent JSONB
        dicts so the Pydantic constructor sees the canonical shape.
        """
        for key in [k for k in row if k.startswith("pipeline_")]:
            del row[key]
        row.pop("held_connection", None)

        # --- access_allowed_ids → access_control.allowed_identity_ids -----
        ac: dict[str, Any] = row["access_control"]
        ac["allowed_identity_ids"] = row.pop("access_allowed_ids", [])

        # --- code_files_changed → code_metadata.files_changed -------------
        cm: dict[str, Any] | None = row.get("code_metadata")
        if cm is not None:
            cm["files_changed"] = row.pop("code_files_changed", [])
        else:
            row.pop("code_files_changed", None)

        return CanonicalEvent(**row)

    async def get_active_contexts_for_repo_compilation(
        self,
        tenant_id: UUID,
        repo_identifier: str,
    ) -> list[dict[str, Any]]:
        """Fetch the active canonical contexts for a repo, for ARCHITECTURE.md compilation.

        Returns id, summary, confidence_score, created_at, is_locked,
        context_type, severity, access_control. Only decision records and blocking
        records are listed. Two sources are unioned:

        1. Records with at least one ``ACTIVE`` ``ast_context_edges`` row into
           ``repo_identifier``.
        2. Records which are scope-eligible for this repo, including those with no
           code symbol to link.
        """
        from sqlalchemy import select

        from checkcord.models.enums import ASTEdgeStatus
        from checkcord.storage.models import ASTContextEdge as DBASTContextEdge
        from checkcord.storage.models import ASTNode as DBASTNode
        from checkcord.storage.models import CanonicalContext as DBCanonicalContext

        columns = (
            DBCanonicalContext.__table__.c.id,
            DBCanonicalContext.__table__.c.summary,
            DBCanonicalContext.__table__.c.summary_redacted,
            DBCanonicalContext.__table__.c.confidence_score,
            DBCanonicalContext.__table__.c.created_at,
            DBCanonicalContext.__table__.c.is_locked,
            DBCanonicalContext.__table__.c.context_type,
            DBCanonicalContext.__table__.c.severity,
            DBCanonicalContext.__table__.c.access_control,
        )
        listed = decisions_or_blocking(DBCanonicalContext.__table__)

        ast_linked_stmt = (
            select(*columns)
            .select_from(
                DBCanonicalContext.__table__.join(
                    DBASTContextEdge.__table__,
                    DBASTContextEdge.__table__.c.canonical_id == DBCanonicalContext.__table__.c.id,
                ).join(
                    DBASTNode.__table__,
                    DBASTNode.__table__.c.id == DBASTContextEdge.__table__.c.ast_node_id,
                )
            )
            .where(
                (DBASTNode.__table__.c.tenant_id == tenant_id)
                & (DBASTContextEdge.__table__.c.tenant_id == tenant_id)
                & (DBCanonicalContext.__table__.c.tenant_id == tenant_id)
                & (DBASTNode.__table__.c.repo_identifier == repo_identifier)
                & (DBASTContextEdge.__table__.c.status == ASTEdgeStatus.ACTIVE.value)
                & listed
            )
            .distinct()
        )

        typed_scope_stmt = select(*columns).where(
            (DBCanonicalContext.__table__.c.tenant_id == tenant_id)
            & listed
            & _scope_filter(tenant_id, repo_identifier=repo_identifier)
        )

        combined = ast_linked_stmt.union(typed_scope_stmt).subquery()
        stmt = select(combined).order_by(
            (combined.c.severity == BLOCKING).desc(),
            combined.c.confidence_score.desc(),
            combined.c.created_at.desc(),
        )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            rows = result.mappings().fetchall()
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                rows = result.mappings().fetchall()

        return [dict(row) for row in rows]

    async def get_repo_ledger_mappings(
        self,
        tenant_id: UUID,
        repo_identifier: str,
    ) -> list[dict[str, Any]]:
        """Fetch active (symbol_name, canonical_id) mappings for a repo."""
        from sqlalchemy import select

        from checkcord.models.enums import ASTEdgeStatus
        from checkcord.storage.models import ASTContextEdge as DBASTContextEdge
        from checkcord.storage.models import ASTNode as DBASTNode

        stmt = (
            select(
                DBASTNode.__table__.c.symbol_name,
                DBASTContextEdge.__table__.c.canonical_id,
            )
            .select_from(
                DBASTNode.__table__.join(
                    DBASTContextEdge.__table__,
                    DBASTNode.__table__.c.id == DBASTContextEdge.__table__.c.ast_node_id,
                )
            )
            .where(
                (DBASTNode.__table__.c.tenant_id == tenant_id)
                & (DBASTContextEdge.__table__.c.tenant_id == tenant_id)
                & (DBASTNode.__table__.c.repo_identifier == repo_identifier)
                & (DBASTContextEdge.__table__.c.status == ASTEdgeStatus.ACTIVE.value)
            )
            .distinct()
        )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            rows = result.mappings().fetchall()
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                rows = result.mappings().fetchall()

        return [dict(row) for row in rows]

    async def get_active_repos_for_compilation(self, tenant_id: UUID) -> list[str]:
        """Fetch distinct repo_identifiers that have active context edges within a tenant."""
        from sqlalchemy import select

        from checkcord.models.enums import ASTEdgeStatus
        from checkcord.storage.models import ASTContextEdge as DBASTContextEdge
        from checkcord.storage.models import ASTNode as DBASTNode

        stmt = (
            select(DBASTNode.__table__.c.repo_identifier)
            .select_from(
                DBASTNode.__table__.join(
                    DBASTContextEdge.__table__,
                    DBASTNode.__table__.c.id == DBASTContextEdge.__table__.c.ast_node_id,
                )
            )
            .where(
                (DBASTNode.__table__.c.tenant_id == tenant_id)
                & (DBASTContextEdge.__table__.c.tenant_id == tenant_id)
                & (DBASTContextEdge.__table__.c.status == ASTEdgeStatus.ACTIVE.value)
            )
            .distinct()
        )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            rows = result.mappings().fetchall()
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                rows = result.mappings().fetchall()

        return [row["repo_identifier"] for row in rows]

    async def get_symbols_for_context_in_repo(
        self,
        tenant_id: UUID,
        canonical_id: UUID,
        repo_identifier: str,
    ) -> list[dict[str, Any]]:
        """Fetch symbols linked to a canonical context within a repository."""
        from sqlalchemy import select

        from checkcord.models.enums import ASTEdgeStatus
        from checkcord.storage.models import ASTContextEdge as DBASTContextEdge
        from checkcord.storage.models import ASTNode as DBASTNode

        stmt = (
            select(
                DBASTNode.__table__.c.symbol_name,
                DBASTNode.__table__.c.file_path,
            )
            .select_from(
                DBASTNode.__table__.join(
                    DBASTContextEdge.__table__,
                    DBASTNode.__table__.c.id == DBASTContextEdge.__table__.c.ast_node_id,
                )
            )
            .where(
                (DBASTNode.__table__.c.tenant_id == tenant_id)
                & (DBASTContextEdge.__table__.c.tenant_id == tenant_id)
                & (DBASTContextEdge.__table__.c.canonical_id == canonical_id)
                & (DBASTNode.__table__.c.repo_identifier == repo_identifier)
                & (DBASTContextEdge.__table__.c.status == ASTEdgeStatus.ACTIVE.value)
            )
            .distinct()
        )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            rows = result.mappings().fetchall()
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                rows = result.mappings().fetchall()

        return [dict(row) for row in rows]

    async def get_network_graph(
        self,
        tenant_id: UUID,
        repo_identifier: str,
    ) -> dict[str, Any]:
        """Retrieve network graph representing Files, Symbols, Contexts, and Developers."""
        from sqlalchemy import select

        from checkcord.models.enums import ASTEdgeStatus
        from checkcord.storage.models import ASTContextEdge as DBASTContextEdge
        from checkcord.storage.models import ASTNode as DBASTNode
        from checkcord.storage.models import CanonicalContext as DBCanonicalContext
        from checkcord.storage.models import ContextEventLink as DBContextEventLink

        # Fetch AST nodes (Files and Symbols)
        nodes_stmt = select(
            DBASTNode.__table__.c.id,
            DBASTNode.__table__.c.file_path,
            DBASTNode.__table__.c.symbol_name,
            DBASTNode.__table__.c.node_type,
        ).where(
            (DBASTNode.__table__.c.tenant_id == tenant_id)
            & (DBASTNode.__table__.c.repo_identifier == repo_identifier)
        )

        # Fetch Active Contexts linked to these AST nodes
        contexts_stmt = (
            select(
                DBASTNode.__table__.c.id.label("ast_node_id"),
                DBCanonicalContext.__table__.c.id.label("context_id"),
                DBCanonicalContext.__table__.c.summary,
                DBCanonicalContext.__table__.c.summary_redacted,
                DBCanonicalContext.__table__.c.confidence_score,
            )
            .select_from(
                DBASTNode.__table__.join(
                    DBASTContextEdge.__table__,
                    DBASTNode.__table__.c.id == DBASTContextEdge.__table__.c.ast_node_id,
                ).join(
                    DBCanonicalContext.__table__,
                    DBASTContextEdge.__table__.c.canonical_id == DBCanonicalContext.__table__.c.id,
                )
            )
            .where(
                (DBASTNode.__table__.c.tenant_id == tenant_id)
                & (DBASTNode.__table__.c.repo_identifier == repo_identifier)
                & (DBASTContextEdge.__table__.c.tenant_id == tenant_id)
                & (DBASTContextEdge.__table__.c.status == ASTEdgeStatus.ACTIVE.value)
                & (DBCanonicalContext.__table__.c.tenant_id == tenant_id)
                & live_records(DBCanonicalContext.__table__)
            )
        )

        # Fetch Developers linked to these Active Contexts
        devs_stmt = (
            select(
                DBCanonicalContext.__table__.c.id.label("context_id"),
                events_table.c.author["display_name"].astext.label("author_name"),
            )
            .select_from(
                DBASTNode.__table__.join(
                    DBASTContextEdge.__table__,
                    DBASTNode.__table__.c.id == DBASTContextEdge.__table__.c.ast_node_id,
                )
                .join(
                    DBCanonicalContext.__table__,
                    DBASTContextEdge.__table__.c.canonical_id == DBCanonicalContext.__table__.c.id,
                )
                .join(
                    DBContextEventLink.__table__,
                    DBCanonicalContext.__table__.c.id
                    == DBContextEventLink.__table__.c.canonical_id,
                )
                .join(
                    events_table,
                    DBContextEventLink.__table__.c.event_id == events_table.c.event_id,
                )
            )
            .where(
                (DBASTNode.__table__.c.tenant_id == tenant_id)
                & (DBASTNode.__table__.c.repo_identifier == repo_identifier)
                & (DBASTContextEdge.__table__.c.tenant_id == tenant_id)
                & (DBASTContextEdge.__table__.c.status == ASTEdgeStatus.ACTIVE.value)
                & (DBCanonicalContext.__table__.c.tenant_id == tenant_id)
                & live_records(DBCanonicalContext.__table__)
                & (DBContextEventLink.__table__.c.tenant_id == tenant_id)
                & (events_table.c.tenant_id == tenant_id)
            )
            .distinct()
        )

        if isinstance(self._bind, AsyncConnection):
            nodes_res = await self._bind.execute(nodes_stmt)
            ast_nodes = nodes_res.mappings().fetchall()

            ctx_res = await self._bind.execute(contexts_stmt)
            contexts = ctx_res.mappings().fetchall()

            dev_res = await self._bind.execute(devs_stmt)
            devs = dev_res.mappings().fetchall()
        else:
            async with self._bind.begin() as conn:
                nodes_res = await conn.execute(nodes_stmt)
                ast_nodes = nodes_res.mappings().fetchall()

                ctx_res = await conn.execute(contexts_stmt)
                contexts = ctx_res.mappings().fetchall()

                dev_res = await conn.execute(devs_stmt)
                devs = dev_res.mappings().fetchall()

        # Build network graph dictionary
        nodes_list = []
        edges_list = []

        seen_nodes = set()
        seen_edges = set()

        # Add Files and AST Symbols
        for row in ast_nodes:
            file_id = f"file:{row['file_path']}"
            if file_id not in seen_nodes:
                nodes_list.append(
                    {
                        "id": file_id,
                        "type": "file",
                        "label": row["file_path"],
                    }
                )
                seen_nodes.add(file_id)

            sym_id = f"symbol:{str(row['id'])}"
            if sym_id not in seen_nodes:
                nodes_list.append(
                    {
                        "id": sym_id,
                        "type": "symbol",
                        "label": row["symbol_name"],
                        "node_type": row["node_type"],
                    }
                )
                seen_nodes.add(sym_id)

            edge_key = (file_id, sym_id)
            if edge_key not in seen_edges:
                edges_list.append(
                    {
                        "source": file_id,
                        "target": sym_id,
                    }
                )
                seen_edges.add(edge_key)

        # Add Contexts
        for row in contexts:
            ctx_id = f"context:{str(row['context_id'])}"
            if ctx_id not in seen_nodes:
                nodes_list.append(
                    {
                        "id": ctx_id,
                        "type": "context",
                        "label": row["summary"],
                        "label_redacted": row["summary_redacted"],
                        "confidence_score": row["confidence_score"],
                    }
                )
                seen_nodes.add(ctx_id)

            sym_id = f"symbol:{str(row['ast_node_id'])}"
            edge_key = (sym_id, ctx_id)
            if edge_key not in seen_edges:
                edges_list.append(
                    {
                        "source": sym_id,
                        "target": ctx_id,
                    }
                )
                seen_edges.add(edge_key)

        # Add Developers
        for row in devs:
            author_name = row["author_name"]
            if not author_name:
                continue
            dev_id = f"developer:{author_name}"
            if dev_id not in seen_nodes:
                nodes_list.append(
                    {
                        "id": dev_id,
                        "type": "developer",
                        "label": author_name,
                    }
                )
                seen_nodes.add(dev_id)

            ctx_id = f"context:{str(row['context_id'])}"
            edge_key = (ctx_id, dev_id)
            if edge_key not in seen_edges:
                edges_list.append(
                    {
                        "source": ctx_id,
                        "target": dev_id,
                    }
                )
                seen_edges.add(edge_key)

        return {
            "nodes": nodes_list,
            "edges": edges_list,
        }

    async def get_node_details(
        self,
        tenant_id: UUID,
        node_id: str,
        repo_identifier: str,
    ) -> dict[str, Any] | None:
        """Retrieve detailed metadata for a single node in the graph."""
        from sqlalchemy import func, select

        from checkcord.models.enums import ASTEdgeStatus
        from checkcord.storage.models import ASTContextEdge as DBASTContextEdge
        from checkcord.storage.models import ASTNode as DBASTNode
        from checkcord.storage.models import CanonicalContext as DBCanonicalContext
        from checkcord.storage.models import ContextEventLink as DBContextEventLink

        if ":" not in node_id:
            return None

        node_type, _, target_val = node_id.partition(":")

        if node_type == "file":
            # Target_val is file_path. Query symbols in this file.
            stmt = select(
                DBASTNode.__table__.c.id,
                DBASTNode.__table__.c.symbol_name,
                DBASTNode.__table__.c.node_type,
            ).where(
                (DBASTNode.__table__.c.tenant_id == tenant_id)
                & (DBASTNode.__table__.c.repo_identifier == repo_identifier)
                & (DBASTNode.__table__.c.file_path == target_val)
            )
            if isinstance(self._bind, AsyncConnection):
                res = await self._bind.execute(stmt)
                rows = res.mappings().fetchall()
            else:
                async with self._bind.begin() as conn:
                    res = await conn.execute(stmt)
                    rows = res.mappings().fetchall()

            return {
                "type": "file",
                "id": node_id,
                "file_path": target_val,
                "repo_identifier": repo_identifier,
                "symbols": [
                    {
                        "id": str(r["id"]),
                        "symbol_name": r["symbol_name"],
                        "node_type": r["node_type"],
                    }
                    for r in rows
                ],
            }

        elif node_type == "symbol":
            # Target_val is ast_node_id UUID
            try:
                node_uuid = UUID(target_val)
            except ValueError:
                return None

            node_stmt = select(
                DBASTNode.__table__.c.id,
                DBASTNode.__table__.c.file_path,
                DBASTNode.__table__.c.symbol_name,
                DBASTNode.__table__.c.node_type,
                DBASTNode.__table__.c.merkle_hash,
            ).where(
                (DBASTNode.__table__.c.tenant_id == tenant_id)
                & (DBASTNode.__table__.c.id == node_uuid)
            )

            # Fetch linked active contexts
            contexts_stmt = (
                select(
                    DBCanonicalContext.__table__.c.id,
                    DBCanonicalContext.__table__.c.summary,
                    DBCanonicalContext.__table__.c.summary_redacted,
                    DBCanonicalContext.__table__.c.confidence_score,
                )
                .select_from(
                    DBASTContextEdge.__table__.join(
                        DBCanonicalContext.__table__,
                        DBASTContextEdge.__table__.c.canonical_id
                        == DBCanonicalContext.__table__.c.id,
                    )
                )
                .where(
                    (DBASTContextEdge.__table__.c.tenant_id == tenant_id)
                    & (DBASTContextEdge.__table__.c.ast_node_id == node_uuid)
                    & (DBASTContextEdge.__table__.c.status == ASTEdgeStatus.ACTIVE.value)
                    & (DBCanonicalContext.__table__.c.tenant_id == tenant_id)
                    & live_records(DBCanonicalContext.__table__)
                )
            )

            if isinstance(self._bind, AsyncConnection):
                node_res = await self._bind.execute(node_stmt)
                node_row = node_res.mappings().fetchone()
                if not node_row:
                    return None
                ctx_res = await self._bind.execute(contexts_stmt)
                contexts = ctx_res.mappings().fetchall()
            else:
                async with self._bind.begin() as conn:
                    node_res = await conn.execute(node_stmt)
                    node_row = node_res.mappings().fetchone()
                    if not node_row:
                        return None
                    ctx_res = await conn.execute(contexts_stmt)
                    contexts = ctx_res.mappings().fetchall()

            return {
                "type": "symbol",
                "id": node_id,
                "symbol_name": node_row["symbol_name"],
                "node_type": node_row["node_type"],
                "file_path": node_row["file_path"],
                "repo_identifier": repo_identifier,
                "merkle_hash": node_row["merkle_hash"],
                "contexts": [
                    {
                        "id": str(r["id"]),
                        "summary": r["summary"],
                        "summary_redacted": r["summary_redacted"],
                        "confidence_score": r["confidence_score"],
                    }
                    for r in contexts
                ],
            }

        elif node_type == "context":
            # Target_val is canonical_id UUID
            try:
                ctx_uuid = UUID(target_val)
            except ValueError:
                return None

            ctx_stmt = select(
                DBCanonicalContext.__table__.c.id,
                DBCanonicalContext.__table__.c.summary,
                DBCanonicalContext.__table__.c.summary_redacted,
                DBCanonicalContext.__table__.c.confidence_score,
                DBCanonicalContext.__table__.c.is_locked,
                DBCanonicalContext.__table__.c.content_hash,
                DBCanonicalContext.__table__.c.last_verified_at,
                DBCanonicalContext.__table__.c.context_type,
            ).where(
                (DBCanonicalContext.__table__.c.tenant_id == tenant_id)
                & (DBCanonicalContext.__table__.c.id == ctx_uuid)
            )

            # Query events linked to this context
            events_stmt = (
                select(
                    events_table.c.event_id,
                    events_table.c.source_platform,
                    events_table.c.source_type,
                    events_table.c.content_clean,
                    events_table.c.author,
                    events_table.c.happened_at,
                    events_table.c.metadata["chat_source"].label("chat_source"),
                )
                .select_from(
                    DBContextEventLink.__table__.join(
                        events_table,
                        DBContextEventLink.__table__.c.event_id == events_table.c.event_id,
                    )
                )
                .where(
                    (DBContextEventLink.__table__.c.tenant_id == tenant_id)
                    & (DBContextEventLink.__table__.c.canonical_id == ctx_uuid)
                    & (events_table.c.tenant_id == tenant_id)
                )
            )

            if isinstance(self._bind, AsyncConnection):
                ctx_res = await self._bind.execute(ctx_stmt)
                ctx_row = ctx_res.mappings().fetchone()
                if not ctx_row:
                    return None
                events_res = await self._bind.execute(events_stmt)
                events = events_res.mappings().fetchall()
                repo_scopes = await self._fetch_repo_scopes_by_canonical_ids(
                    self._bind, tenant_id, [ctx_uuid]
                )
            else:
                async with self._bind.begin() as conn:
                    ctx_res = await conn.execute(ctx_stmt)
                    ctx_row = ctx_res.mappings().fetchone()
                    if not ctx_row:
                        return None
                    events_res = await conn.execute(events_stmt)
                    events = events_res.mappings().fetchall()
                    repo_scopes = await self._fetch_repo_scopes_by_canonical_ids(
                        conn, tenant_id, [ctx_uuid]
                    )

            return {
                "type": "context",
                "id": node_id,
                "summary": ctx_row["summary"],
                "summary_redacted": ctx_row["summary_redacted"],
                "confidence_score": ctx_row["confidence_score"],
                "scope_repo_identifiers": repo_scopes[ctx_uuid],
                "is_locked": ctx_row["is_locked"],
                "context_type": ctx_row["context_type"],
                "content_hash": ctx_row["content_hash"],
                "last_verified_at": (
                    ctx_row["last_verified_at"].isoformat() if ctx_row["last_verified_at"] else None
                ),
                "sources": [
                    {
                        "event_id": str(r["event_id"]),
                        "source_platform": r["source_platform"],
                        "source_type": r["source_type"],
                        "content_clean": r["content_clean"],
                        "author": r["author"],
                        "happened_at": r["happened_at"].isoformat() if r["happened_at"] else None,
                        **({"chat_source": r["chat_source"]} if r["chat_source"] else {}),
                    }
                    for r in events
                ],
            }

        elif node_type == "developer":
            # Target_val is developer display name
            dev_stmt = select(func.count(events_table.c.event_id)).where(
                (events_table.c.tenant_id == tenant_id)
                & (events_table.c.author["display_name"].astext == target_val)
            )

            if isinstance(self._bind, AsyncConnection):
                dev_res = await self._bind.execute(dev_stmt)
                count = dev_res.scalar()
            else:
                async with self._bind.begin() as conn:
                    dev_res = await conn.execute(dev_stmt)
                    count = dev_res.scalar()

            return {
                "type": "developer",
                "id": node_id,
                "display_name": target_val,
                "authored_events_count": count or 0,
            }

        return None

    async def list_graph_repositories(self, tenant_id: UUID) -> list[tuple[str, int]]:
        """Repositories with a code graph, with their file counts, by name."""
        from sqlalchemy import func, select

        from checkcord.storage.models import ASTNode as DBASTNode

        c = DBASTNode.__table__.c
        stmt = (
            select(c.repo_identifier, func.count(func.distinct(c.file_path)))
            .where(c.tenant_id == tenant_id)
            .group_by(c.repo_identifier)
            .order_by(c.repo_identifier)
        )
        if isinstance(self._bind, AsyncConnection):
            rows = (await self._bind.execute(stmt)).all()
        else:
            async with self._bind.begin() as conn:
                rows = (await conn.execute(stmt)).all()
        return [(repo, int(files)) for repo, files in rows]

    async def check_repo_access(self, tenant_id: UUID, repo_identifier: str) -> bool:
        """Check if a tenant has access to a repository by seeing if any AST nodes exist for it."""
        from sqlalchemy import select

        from checkcord.storage.models import ASTNode as DBASTNode

        stmt = (
            select(DBASTNode.__table__.c.id)
            .where(
                (DBASTNode.__table__.c.tenant_id == tenant_id)
                & (DBASTNode.__table__.c.repo_identifier == repo_identifier)
            )
            .limit(1)
        )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            row = result.fetchone()
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                row = result.fetchone()

        return row is not None

    async def upsert_workflow_definition(
        self,
        tenant_id: UUID,
        repo_identifier: str,
        workflow_path: str,
        name: str | None,
        jobs: list[dict[str, Any]],
        commit_sha: str | None,
    ) -> None:
        """Upsert a workflow's parsed job/step graph.

        Keyed on (tenant_id, repo_identifier, workflow_path); a refresh
        overwrites the prior definition.
        """
        from checkcord.storage.models import WorkflowDefinitionRecord

        row = {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "repo_identifier": repo_identifier,
            "workflow_path": workflow_path,
            "name": name,
            "jobs": jobs,
            "commit_sha": commit_sha,
            "updated_at": datetime.now(UTC),
        }
        stmt = (
            pg_insert(WorkflowDefinitionRecord.__table__)
            .values(row)
            .on_conflict_do_update(
                index_elements=["tenant_id", "repo_identifier", "workflow_path"],
                set_={
                    "name": name,
                    "jobs": jobs,
                    "commit_sha": commit_sha,
                    "updated_at": datetime.now(UTC),
                },
            )
        )

        if isinstance(self._bind, AsyncConnection):
            await self._bind.execute(stmt)
        else:
            async with self._bind.begin() as conn:
                await conn.execute(stmt)

    async def create_ci_failure_context_link(
        self,
        tenant_id: UUID,
        ci_event_id: UUID,
        canonical_id: UUID,
        file_path: str,
        ast_node_id: UUID | None = None,
    ) -> bool:
        """Link a CI failure event to an affected record or AST node at file granularity.

        Idempotent on (ci_event_id, canonical_id, file_path). Returns True if a
        row was inserted, False if it already existed.
        """
        from checkcord.storage.models import CIFailureContextLink

        row = {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "ci_event_id": ci_event_id,
            "canonical_id": canonical_id,
            "ast_node_id": ast_node_id,
            "file_path": file_path,
            "created_at": datetime.now(UTC),
        }
        stmt = (
            pg_insert(CIFailureContextLink.__table__)
            .values(row)
            .on_conflict_do_nothing(
                index_elements=["ci_event_id", "canonical_id", "file_path"],
            )
        )

        if isinstance(self._bind, AsyncConnection):
            result = await self._bind.execute(stmt)
            inserted = (result.rowcount or 0) > 0
        else:
            async with self._bind.begin() as conn:
                result = await conn.execute(stmt)
                inserted = (result.rowcount or 0) > 0

        return inserted
