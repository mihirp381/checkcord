"""Admin endpoints: unredacted content, the audit log, purges, usage and review of held material.

The caller is an admin key or a signed-in admin. Every call here writes an audit entry.
"""

import csv
import io
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.api.deps import Principal, get_db_engine, require_admin_principal
from checkcord.api.graph_router import node_record_ids
from checkcord.api.schemas import ERRORS, Page
from checkcord.services import purge, records, review, stats, usage
from checkcord.storage import audit
from checkcord.storage.postgres import PostgresEventStore, tenant_context

logger = structlog.get_logger(__name__).bind(component="admin_router")

router = APIRouter(prefix="/api/v1/admin", responses=ERRORS)


@router.get(
    "/raw-content/{node_id:path}", tags=["admin: records"], summary="A graph node unredacted"
)
async def get_raw_node_details(
    node_id: str,
    repo_identifier: str = Query(..., description="The repository identifier context"),
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, Any]:
    """Return a graph node's details unredacted.

    Same lookup as ``GET /api/v1/graph/node/{node_id}`` without the redaction
    pass; the only endpoint that returns unredacted ``summary`` and
    ``content_clean`` text. Each source carries its id, platform, time and text, and a chat
    outcome source the messages it cites (author, time, text).
    """
    tenant_id = principal.tenant_id
    async with tenant_context(db_engine, tenant_id) as conn:
        store = PostgresEventStore(conn)
        details = await store.get_node_details(
            tenant_id=tenant_id,
            node_id=node_id,
            repo_identifier=repo_identifier,
        )

        if not details:
            raise HTTPException(
                status_code=404, detail=f"Node {node_id} not found or access denied"
            )
        if details.get("sources"):
            details["sources"] = await records.raw_sources(conn, tenant_id, details["sources"])

        await audit.record(
            conn,
            tenant_id,
            principal.as_actor(),
            "admin.raw_content",
            params={"node": node_id, "repo": repo_identifier},
            record_ids=node_record_ids(details),
            result_count=1,
        )

    return _without_stored_redaction(details)


def _without_stored_redaction(details: dict[str, Any]) -> dict[str, Any]:
    raw = {k: v for k, v in details.items() if k != "summary_redacted"}
    if "contexts" in raw:
        raw["contexts"] = [
            {k: v for k, v in c.items() if k != "summary_redacted"} for c in raw["contexts"]
        ]
    return raw


def _filters(
    since: datetime | None = Query(None, description="Entries at or after this time"),
    until: datetime | None = Query(None, description="Entries before this time"),
    user: str | None = Query(None, description="User id or email"),
    key: str | None = Query(None, description="API key id or name"),
    action: str | None = Query(None, description="Exact action, e.g. mcp.view_active_decisions"),
    channel: Literal["mcp", "api", "cli", "scim"] | None = Query(None),
    record_id: str | None = Query(None, description="Entries that returned this record"),
) -> audit.Filters:
    return audit.Filters(
        since=since,
        until=until,
        user=user,
        key=key,
        action=action,
        channel=channel,
        record_id=record_id,
    )


@router.get("/audit", tags=["admin: audit"], summary="List audit entries")
async def list_audit_entries(
    filters: audit.Filters = Depends(_filters),
    limit: int = Query(100, ge=1, le=1000),
    cursor: str | None = Query(None, description="next_cursor from the previous page"),
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> Page[dict[str, Any]]:
    """Audit entries of the caller's tenant, newest first."""
    tenant_id = principal.tenant_id
    async with tenant_context(db_engine, tenant_id) as conn:
        try:
            rows, next_cursor = await audit.list_entries(
                conn, tenant_id, filters, limit=limit, cursor=cursor
            )
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err
        await audit.record(
            conn,
            tenant_id,
            principal.as_actor(),
            "audit.read",
            params={**filters.params(), "cursor": cursor, "limit": limit},
            result_count=len(rows),
        )
    return Page(items=[audit.jsonable(r) for r in rows], next_cursor=next_cursor)


@router.get(
    "/audit/export",
    tags=["admin: audit"],
    summary="Export audit entries",
    response_class=StreamingResponse,
)
async def export_audit_entries(
    filters: audit.Filters = Depends(_filters),
    format: Literal["jsonl", "csv"] = Query("jsonl"),
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> StreamingResponse:
    """Stream the tenant's matching entries, oldest first, as JSON Lines or CSV."""
    tenant_id = principal.tenant_id
    # Recorded before streaming starts, so an export that cannot be recorded never begins.
    async with tenant_context(db_engine, tenant_id) as conn:
        await audit.record(
            conn,
            tenant_id,
            principal.as_actor(),
            "audit.export",
            params={**filters.params(), "format": format},
        )

    async def body() -> AsyncIterator[str]:
        async with tenant_context(db_engine, tenant_id) as conn:
            if format == "csv":
                yield _csv_line(audit.COLUMNS)
            async for row in audit.stream_entries(conn, tenant_id, filters):
                if format == "csv":
                    yield _csv_line(audit.csv_row(row))
                else:
                    yield json.dumps(audit.jsonable(row)) + "\n"

    media_type = "text/csv" if format == "csv" else "application/x-ndjson"
    filename = f"checkcord-audit.{format}"
    return StreamingResponse(
        body(),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _csv_line(values: Any) -> str:
    out = io.StringIO()
    csv.writer(out).writerow(values)
    return out.getvalue()


class PurgeSource(BaseModel):
    platform: str
    container: str
    dry_run: bool = False


class PurgeEvents(BaseModel):
    event_ids: list[UUID] = Field(min_length=1, max_length=1000)
    dry_run: bool = False


class PurgeRecord(BaseModel):
    record_id: UUID
    dry_run: bool = False


async def run_purge(
    request: Request,
    principal: Principal,
    db_engine: AsyncEngine,
    selection: purge.Selection,
    dry_run: bool,
) -> dict[str, Any]:
    if dry_run:
        found = await purge.count(db_engine, principal.tenant_id, selection)
        return {
            "dry_run": True,
            "events": found.events,
            "records_deleted": found.records_deleted,
            "records_updated": found.records_updated,
            "meeting_segments": found.segments,
            "code_symbols": found.code_symbols,
        }
    ctx = getattr(request.app.state, "ctx", None)
    result = await purge.purge(
        db_engine,
        principal.tenant_id,
        selection,
        actor=principal.as_actor(),
        redis=ctx.redis_client if ctx else None,
    )
    return {"dry_run": False, **result.counts()}


@router.post("/purge/source", tags=["admin: purge"], summary="Purge a source")
async def purge_source(
    body: PurgeSource,
    request: Request,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, Any]:
    """Delete every event of one channel, repository or space and what was made from it."""
    selection = purge.source(body.platform, body.container)
    return await run_purge(request, principal, db_engine, selection, body.dry_run)


@router.post("/purge/event", tags=["admin: purge"], summary="Purge events")
async def purge_events(
    body: PurgeEvents,
    request: Request,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, Any]:
    """Delete events and what was made only from them."""
    selection = purge.events(body.event_ids)
    return await run_purge(request, principal, db_engine, selection, body.dry_run)


@router.post("/purge/record", tags=["admin: purge"], summary="Purge a record")
async def purge_record(
    body: PurgeRecord,
    request: Request,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, Any]:
    """Delete a record and the events linked only to it."""
    selection = purge.record(body.record_id)
    return await run_purge(request, principal, db_engine, selection, body.dry_run)


@router.get("/review", tags=["admin: review"], summary="List held records")
async def list_held_records(
    connection: str | None = Query(None, description="Only records held under this connection"),
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> Page[dict[str, Any]]:
    """Records held for review, with redacted summaries, their sources and counts."""
    held = await review.list_held(
        db_engine, principal.tenant_id, principal.as_actor(), connection=connection
    )
    return Page(items=held)


class ReviewTarget(BaseModel):
    connection: str | None = None
    record_id: UUID | None = None
    record_ids: list[UUID] | None = Field(None, min_length=1, max_length=500)


def _target(body: ReviewTarget) -> ReviewTarget:
    given = [v for v in (body.connection, body.record_id, body.record_ids) if v is not None]
    if len(given) != 1:
        raise HTTPException(
            status_code=422, detail="give exactly one of connection, record_id, record_ids"
        )
    return body


def _redis(request: Request) -> Any:
    ctx = getattr(request.app.state, "ctx", None)
    return ctx.redis_client if ctx else None


def require_redis(request: Request) -> Any:
    redis = _redis(request)
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis is not available")
    return redis


@router.get("/usage", tags=["admin: activity"], summary="Model usage")
async def model_usage(
    days: int = Query(30, ge=1, le=366),
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, Any]:
    """Model calls and tokens by kind, purpose and model; cost when MODEL_PRICES is set."""
    return await usage.report(db_engine, principal.tenant_id, days, principal.as_actor())


@router.get("/stats", tags=["admin: activity"], summary="Activity summary")
async def activity_stats(
    request: Request,
    days: int = Query(7, ge=1, le=366),
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, Any]:
    """Events stored, by arrival day; records; waiting and failed chat discussions; agent reads,
    pull-request checks and connections."""
    return await stats.summary(
        db_engine, principal.tenant_id, days, principal.as_actor(), _redis(request)
    )


@router.post("/review/release", tags=["admin: review"], summary="Release held material")
async def release_held(
    body: ReviewTarget,
    request: Request,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, Any]:
    """Release a connection (which also makes it live) or one held record."""
    target = _target(body)
    tenant_id, actor = principal.tenant_id, principal.as_actor()
    if target.connection is not None:
        released = await review.release_connection(
            db_engine, tenant_id, target.connection, actor, _redis(request)
        )
    elif target.record_ids is not None:
        released, not_held = review.Released(), []
        for record_id in dict.fromkeys(target.record_ids):
            if any(str(r["id"]) == str(record_id) for r in released.records):
                continue
            one = await review.release_record(
                db_engine, tenant_id, record_id, actor, _redis(request)
            )
            if one is None:
                not_held.append(str(record_id))
                continue
            released.events += one.events
            released.records.extend(one.records)
        return {
            **released.counts(),
            "record_ids": [str(r["id"]) for r in released.records],
            "not_held": not_held,
        }
    else:
        assert target.record_id is not None
        found = await review.release_record(
            db_engine, tenant_id, target.record_id, actor, _redis(request)
        )
        if found is None:
            raise HTTPException(status_code=404, detail="no held record with that id")
        released = found
    return {**released.counts(), "record_ids": [str(r["id"]) for r in released.records]}


@router.post("/review/reject", tags=["admin: review"], summary="Reject held material")
async def reject_held(
    body: ReviewTarget,
    request: Request,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, Any]:
    """Delete a connection's held material, or one held record and the events only it uses."""
    target = _target(body)
    tenant_id, actor = principal.tenant_id, principal.as_actor()
    if target.connection is not None:
        purged = await review.reject_connection(
            db_engine, tenant_id, target.connection, actor, _redis(request)
        )
    elif target.record_ids is not None:
        totals: dict[str, int] = {}
        not_held = []
        for record_id in dict.fromkeys(target.record_ids):
            one = await review.reject_record(
                db_engine, tenant_id, record_id, actor, _redis(request)
            )
            if one is None:
                not_held.append(str(record_id))
                continue
            for name, n in one.counts().items():
                totals[name] = totals.get(name, 0) + n
        return {**totals, "not_held": not_held}
    else:
        assert target.record_id is not None
        found = await review.reject_record(
            db_engine, tenant_id, target.record_id, actor, _redis(request)
        )
        if found is None:
            raise HTTPException(status_code=404, detail="no held record with that id")
        purged = found
    return purged.counts()


class ClearRemoved(BaseModel):
    platform: str | None = None


@router.get("/purge/removed", tags=["admin: purge"], summary="Count removed items remembered")
async def removed_count(
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, Any]:
    """Removed items kept out of ingestion, per platform."""
    counts = await purge.removed_counts(db_engine, principal.tenant_id)
    return {"platforms": counts, "total": sum(counts.values())}


@router.post("/purge/removed/clear", tags=["admin: purge"], summary="Forget removed items")
async def removed_clear(
    body: ClearRemoved,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, Any]:
    """Forget removed items, of one platform or all, so a later delivery is stored again."""
    cleared = await purge.clear_removed(
        db_engine, principal.tenant_id, actor=principal.as_actor(), platform=body.platform
    )
    return {"cleared": cleared}
