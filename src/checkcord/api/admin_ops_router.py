"""Admin endpoints for connections, audiences, the pipeline, records and health."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field, model_validator
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.api.admin_router import _redis, require_redis, run_purge
from checkcord.api.deps import Principal, get_db_engine, require_admin_principal
from checkcord.api.schemas import ERRORS, Page, decode_cursor, encode_cursor
from checkcord.bus import dead_letter
from checkcord.core.config import get_settings
from checkcord.services import (
    audiences,
    connection_audiences,
    connections,
    dead_letters,
    doctor,
    events,
    purge,
    records,
    review,
    scope,
)
from checkcord.storage import audit
from checkcord.storage.postgres import tenant_context

router = APIRouter(prefix="/api/v1/admin", responses=ERRORS)

_CONN, _AUD, _EVENTS, _DLQ, _REC, _HEALTH = (
    ["admin: connections"],
    ["admin: audiences"],
    ["admin: events"],
    ["admin: dead letters"],
    ["admin: records"],
    ["admin: health"],
)


def _connection(value: str) -> tuple[str, str]:
    provider, _, identifier = value.partition(":")
    if not provider or not identifier.strip():
        raise HTTPException(status_code=422, detail="connection is PROVIDER:IDENTIFIER")
    return provider, identifier.strip()


class ConnectionOut(BaseModel):
    connection: str
    provider: str
    identifier: str
    status: Literal["beta", "experimental"] | None = Field(
        description="The connector's status; none for a provider Checkcord does not know"
    )
    state: Literal["live", "review"]
    created_at: datetime | None
    last_event_at: datetime | None
    events: int = Field(description="Events ingested through it, held or not")
    held_records: int = Field(description="Records held for review")
    scope: ScopeLists | None = Field(description="None when the connector has no scope")
    scope_skipped: int = Field(description="Events kept out by the scope in the last 7 days")
    audience_from: Literal["source", "event", "admin"] | None = Field(
        None, description="Where its events' audience comes from"
    )
    audience: list[ConnectionAudienceOut] = Field(
        default_factory=list, description="Admin-set audiences; empty means nobody"
    )


class Recompute(BaseModel):
    state: str | None = Field(description="pending, running or done")
    done: int
    total: int
    finished_at: datetime | None


class ConnectionAudienceOut(BaseModel):
    connection: str
    container: str | None = Field(description="None: the whole connection")
    mode: Literal["nobody", "everyone", "groups", "people"]
    groups: list[str]
    people: list[str]
    members: int
    updated_by: str | None
    updated_at: datetime | None
    recompute: Recompute


class ConnectionAudienceChange(BaseModel):
    connection: str = Field(description="PROVIDER:IDENTIFIER")
    container: str | None = Field(None, description="A Jira project, Linear team or Notion parent")
    mode: Literal["nobody", "everyone", "groups", "people"]
    groups: list[str] = Field(default_factory=list, description="SCIM group names")
    people: list[str] = Field(default_factory=list, description="Emails")


class ScopeLists(BaseModel):
    include: list[str] = Field(description="Non-empty: only these containers are ingested")
    exclude: list[str] = Field(description="Never ingested; wins over include")


class ScopeOut(ScopeLists):
    connection: str
    only_listed: bool
    skipped: int = Field(description="Events kept out in the last `days` days")
    days: int


class ScopeChange(BaseModel):
    connection: str = Field(description="PROVIDER:IDENTIFIER")
    include: list[str] | None = Field(None, description="Replaces the include list")
    exclude: list[str] | None = Field(None, description="Replaces the exclude list")


class ReviewSwitch(BaseModel):
    connection: str = Field(description="PROVIDER:IDENTIFIER")
    in_review: bool


class Disconnect(BaseModel):
    connection: str = Field(description="PROVIDER:IDENTIFIER")
    purge: bool = Field(False, description="Also delete what was stored from it")
    dry_run: bool = False


class InstallLinkRequest(BaseModel):
    provider: Literal["slack", "teams", "notion", "linear"]


class InstallLinkOut(BaseModel):
    provider: str
    url: str = Field(description="Works once")
    redirect_uri: str
    expires_in_seconds: int


@router.get("/connections", tags=_CONN, summary="List connections")
async def list_connections(
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> Page[ConnectionOut]:
    """Each connected source with its state and the time of its newest event. No secrets."""
    from checkcord import connectors

    rows = await connections.with_activity(db_engine, principal.tenant_id)
    settings = await connection_audiences.list_settings(db_engine, principal.tenant_id)
    out = []
    for r in rows:
        found = connectors.get(r["provider"])
        out.append(
            ConnectionOut(
                **r,
                audience_from=found.audience_from if found else None,
                audience=[
                    ConnectionAudienceOut(**s)
                    for s in settings
                    if s["connection"] == r["connection"]
                ],
            )
        )
    return Page(items=out)


@router.get("/connections/audience", tags=_CONN, summary="Show a connection's audience")
async def show_connection_audience(
    connection: str = Query(description="PROVIDER:IDENTIFIER"),
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> Page[ConnectionAudienceOut]:
    """The connection's setting and any per-container ones. None stored means nobody."""
    key = connections.connection_key(*_connection(connection))
    settings = await connection_audiences.list_settings(db_engine, principal.tenant_id)
    return Page(items=[ConnectionAudienceOut(**s) for s in settings if s["connection"] == key])


@router.put("/connections/audience", tags=_CONN, summary="Set a connection's audience")
async def set_connection_audience(
    body: ConnectionAudienceChange,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> ConnectionAudienceOut:
    """For Jira, Notion, Linear, Sentry and Datadog. What was already ingested is brought in
    line in the background; `recompute` shows how far it got."""
    key = connections.connection_key(*_connection(body.connection))
    try:
        setting = await connection_audiences.set_audience(
            db_engine,
            principal.tenant_id,
            key,
            body.mode,
            principal.as_actor(),
            container=body.container,
            groups=body.groups,
            people=body.people,
        )
    except (ValueError, LookupError) as err:
        raise _scope_error(err) from err
    background.add_task(
        connection_audiences.recompute,
        db_engine,
        principal.tenant_id,
        key,
        setting.container,
        redis=_redis(request),
    )
    stored = await connection_audiences.list_settings(db_engine, principal.tenant_id)
    return ConnectionAudienceOut(
        **next(s for s in stored if s["connection"] == key and s["container"] == setting.container)
    )


@router.post("/connections/review", tags=_CONN, summary="Turn review on or off")
async def set_review(
    body: ReviewSwitch,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, Any]:
    """Already held material stays held until it is released or rejected."""
    provider, identifier = _connection(body.connection)
    key = connections.connection_key(provider, identifier)
    await review.set_review(
        db_engine, principal.tenant_id, key, body.in_review, principal.as_actor()
    )
    return {"connection": key, "state": "review" if body.in_review else "live"}


def _scope_error(err: Exception) -> HTTPException:
    return HTTPException(status_code=404 if isinstance(err, LookupError) else 422, detail=str(err))


@router.get("/connections/scope", tags=_CONN, summary="Show a connection's scope")
async def show_scope(
    connection: str = Query(description="PROVIDER:IDENTIFIER"),
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> ScopeOut:
    key = connections.connection_key(*_connection(connection))
    try:
        return ScopeOut(**await scope.show(db_engine, principal.tenant_id, key))
    except ValueError as err:
        raise _scope_error(err) from err


@router.put("/connections/scope", tags=_CONN, summary="Set a connection's scope")
async def set_scope(
    body: ScopeChange,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> ScopeOut:
    """Applies to what arrives from now on. Material already ingested stays; purge it with
    `POST /api/v1/admin/purge/source`."""
    key = connections.connection_key(*_connection(body.connection))
    try:
        await scope.change(
            db_engine,
            principal.tenant_id,
            key,
            principal.as_actor(),
            include=body.include,
            exclude=body.exclude,
        )
        return ScopeOut(**await scope.show(db_engine, principal.tenant_id, key))
    except (ValueError, LookupError) as err:
        raise _scope_error(err) from err


@router.post("/connections/disconnect", tags=_CONN, summary="Disconnect a source")
async def disconnect(
    body: Disconnect,
    request: Request,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, Any]:
    """Deletes the stored installation; Slack's token is revoked first."""
    provider, identifier = _connection(body.connection)
    if provider not in connections.providers():
        raise HTTPException(status_code=422, detail=f"unknown provider {provider!r}")
    selection = purge.connection(provider, identifier)
    if body.dry_run:
        counts = await run_purge(request, principal, db_engine, selection, dry_run=True)
        return {"disconnected": False, "purged": counts}
    removed = await connections.disconnect(
        db_engine, principal.tenant_id, provider, identifier, get_settings(), principal.as_actor()
    )
    if not removed and not body.purge:
        raise HTTPException(status_code=404, detail="no such connection")
    purged = (
        await run_purge(request, principal, db_engine, selection, dry_run=False)
        if body.purge
        else None
    )
    return {"disconnected": removed, "purged": purged}


@router.post("/connections/install-link", tags=_CONN, summary="Make a one-time install link")
async def install_link(
    body: InstallLinkRequest,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> InstallLinkOut:
    """For Slack, Teams, Notion and Linear. Needs PUBLIC_BASE_URL and the provider's OAuth app."""
    settings = get_settings()
    if not settings.public_base_url:
        raise HTTPException(status_code=400, detail="PUBLIC_BASE_URL is not set")
    try:
        link = connections.install_link(
            settings, body.provider, principal.tenant_id, settings.public_base_url
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    async with tenant_context(db_engine, principal.tenant_id) as conn:
        await audit.record(
            conn,
            principal.tenant_id,
            principal.as_actor(),
            "connections.install_link",
            params={"provider": body.provider},
        )
    return InstallLinkOut(
        provider=link.provider,
        url=link.url,
        redirect_uri=link.redirect_uri,
        expires_in_seconds=link.expires_in_seconds,
    )


class AudienceOut(BaseModel):
    platform: str
    container: str
    is_public: bool
    member_count: int = Field(description="Members matched to a person")
    unmatched_count: int = Field(0, description="Members not matched to a person, left out")
    refreshed_at: datetime | None
    attempted_at: datetime | None
    last_error: str | None


class AudienceRefresh(BaseModel):
    platform: str | None = None
    container: str | None = None
    all: bool = False

    @model_validator(mode="after")
    def _one_target(self) -> AudienceRefresh:
        if self.all == bool(self.platform and self.container):
            raise ValueError("give platform and container, or all=true")
        return self


class RefreshOutcome(BaseModel):
    platform: str
    container: str
    is_public: bool
    member_count: int
    kept_last_known: bool
    events_updated: int
    records_updated: int
    error: str | None


@router.get("/audiences", tags=_AUD, summary="List stored audiences")
async def list_audiences(
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> Page[AudienceOut]:
    """Member counts only; member ids are not returned."""
    rows = await audiences.list_stored(db_engine, principal.tenant_id)
    return Page(
        items=[
            AudienceOut(
                platform=r["platform"],
                container=r["container"],
                is_public=bool(r["is_public"]),
                member_count=len(r["member_ids"] or []),
                unmatched_count=r.get("unmatched_count") or 0,
                refreshed_at=r["refreshed_at"],
                attempted_at=r["attempted_at"],
                last_error=r["last_error"],
            )
            for r in rows
        ]
    )


@router.post("/audiences/refresh", tags=_AUD, summary="Refresh one or all audiences")
async def refresh_audiences(
    body: AudienceRefresh,
    request: Request,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> Page[RefreshOutcome]:
    outcomes = await audiences.refresh_selected(
        db_engine,
        principal.tenant_id,
        None if body.all else body.platform,
        None if body.all else body.container,
        trigger="api",
        redis=_redis(request),
        actor=principal.as_actor(),
    )
    return Page(
        items=[
            RefreshOutcome(
                platform=o.platform,
                container=o.container,
                is_public=o.applied.after.is_public,
                member_count=len(o.applied.after.member_ids),
                kept_last_known=o.applied.kept,
                events_updated=o.applied.events,
                records_updated=o.applied.records,
                error=o.error,
            )
            for o in outcomes
        ]
    )


class FailedEvent(BaseModel):
    event_id: UUID
    source: str
    ingested_at: datetime
    attempts: int
    last_error: str | None


class FailedEvents(Page[FailedEvent]):
    retrying: int = Field(description="Events still in the retry sweep")


class EventRetry(BaseModel):
    event_ids: list[UUID] | None = Field(None, max_length=1000)
    all: bool = False

    @model_validator(mode="after")
    def _one_target(self) -> EventRetry:
        if self.all == bool(self.event_ids):
            raise ValueError("give event_ids, or all=true")
        return self


class Retried(BaseModel):
    retried: list[UUID]
    not_failed: list[UUID]


@router.get("/events/failed", tags=_EVENTS, summary="List failed events")
async def failed_events(
    limit: int = Query(100, ge=1, le=500),
    cursor: str | None = Query(None),
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> FailedEvents:
    """Oldest first. Event content is not returned."""
    failed, retrying = await events.list_failed(
        db_engine, principal.tenant_id, limit=limit + 1, after=decode_cursor(cursor)
    )
    page = failed[:limit]
    more = len(failed) > limit
    return FailedEvents(
        items=[
            FailedEvent(
                event_id=e.event_id,
                source=e.source,
                ingested_at=e.ingested_at,
                attempts=e.attempts,
                last_error=e.last_error,
            )
            for e in page
        ],
        next_cursor=encode_cursor((page[-1].ingested_at, page[-1].event_id)) if more else None,
        retrying=retrying,
    )


@router.post("/events/retry", tags=_EVENTS, summary="Retry failed events")
async def retry_events(
    body: EventRetry,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> Retried:
    retried = await events.retry(
        db_engine,
        principal.tenant_id,
        None if body.all else body.event_ids,
        principal.as_actor(),
    )
    missing = [i for i in body.event_ids or [] if i not in set(retried)]
    return Retried(retried=retried, not_failed=missing)


Stream = Annotated[
    str, Path(pattern=r"^[\w.:\-]+\.dlq$", description="A stream name ending in .dlq")
]


class DeadLetterStreamOut(BaseModel):
    name: str
    entries: int
    oldest_age_seconds: int | None


class DeadLetterOut(BaseModel):
    entry_id: str
    age_seconds: int
    event_id: str | None
    error: str | None


class DeadLetterAction(BaseModel):
    entry_ids: list[str] | None = Field(None, max_length=1000)
    all: bool = False

    @model_validator(mode="after")
    def _one_target(self) -> DeadLetterAction:
        if self.all == bool(self.entry_ids):
            raise ValueError("give entry_ids, or all=true")
        return self


class DeadLetterDone(BaseModel):
    stream: str
    done: list[str]


@router.get("/dlq", tags=_DLQ, summary="List dead-letter streams")
async def dlq_streams(
    principal: Principal = Depends(require_admin_principal),
    redis: Redis = Depends(require_redis),
) -> Page[DeadLetterStreamOut]:
    """Counts only the caller's tenant's entries."""
    found = await dead_letters.streams(redis, principal.tenant_id, get_settings().default_tenant_id)
    return Page(
        items=[
            DeadLetterStreamOut(
                name=s.name,
                entries=s.entries,
                oldest_age_seconds=int(dead_letter.entry_age_seconds(s.oldest_id))
                if s.oldest_id
                else None,
            )
            for s in found
        ]
    )


@router.get("/dlq/{stream}/entries", tags=_DLQ, summary="List dead-letter entries")
async def dlq_entries(
    stream: Stream,
    limit: int = Query(100, ge=1, le=500),
    cursor: str | None = Query(None, description="next_cursor from the previous page"),
    principal: Principal = Depends(require_admin_principal),
    redis: Redis = Depends(require_redis),
) -> Page[DeadLetterOut]:
    """Entry id, age, event id and the error line. Message content is never returned."""
    try:
        page, after = await dead_letters.entries(
            redis,
            stream,
            principal.tenant_id,
            get_settings().default_tenant_id,
            limit=limit,
            after=cursor,
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    return Page(items=[DeadLetterOut(**dead_letters.describe(e)) for e in page], next_cursor=after)


async def _dlq_act(
    action: str,
    stream: str,
    body: DeadLetterAction,
    principal: Principal,
    db_engine: AsyncEngine,
    redis: Redis,
) -> DeadLetterDone:
    done = await dead_letters.act(
        db_engine,
        redis,
        action,
        stream,
        principal.tenant_id,
        get_settings().default_tenant_id,
        None if body.all else body.entry_ids,
        principal.as_actor(),
    )
    return DeadLetterDone(stream=stream, done=done)


@router.post("/dlq/{stream}/replay", tags=_DLQ, summary="Replay dead-letter entries")
async def dlq_replay(
    body: DeadLetterAction,
    stream: Stream,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
    redis: Redis = Depends(require_redis),
) -> DeadLetterDone:
    """Puts the tenant's entries back on the source stream."""
    return await _dlq_act("replay", stream, body, principal, db_engine, redis)


@router.post("/dlq/{stream}/drop", tags=_DLQ, summary="Drop dead-letter entries")
async def dlq_drop(
    body: DeadLetterAction,
    stream: Stream,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
    redis: Redis = Depends(require_redis),
) -> DeadLetterDone:
    return await _dlq_act("drop", stream, body, principal, db_engine, redis)


class RecordSummary(BaseModel):
    id: UUID
    context_type: str | None
    severity: Literal["blocking", "advisory"]
    summary: str
    state: Literal["live", "held", "retired"]
    held_connection: str | None
    retired_at: datetime | None
    replaced_by: UUID | None
    confidence_score: float | None
    created_at: datetime


class RecordSource(BaseModel):
    platform: str | None
    container: str | None
    events: int


class ScopeEntry(BaseModel):
    scope_type: str
    scope_value: str


class SupportingEvents(BaseModel):
    count: int = Field(description="Events linked to the record, the origin event included")
    first: datetime | None
    last: datetime | None


class SuggestedReplacement(BaseModel):
    id: UUID
    replacement_id: UUID
    basis: Literal["stated", "model"]


class UpdateSource(BaseModel):
    platform: str | None
    type: str | None


class RecordUpdate(BaseModel):
    happened_at: datetime | None
    source: UpdateSource
    text: str = Field(description="The redacted start of a later event that agrees with it")
    audience: str | None = Field(None, description="Who may read the event: public or N people")


class ReplacementNote(BaseModel):
    record_id: UUID
    created_at: datetime | None
    basis: Literal["stated", "model"]
    summary: str = Field(description="The newer record's redacted summary, cut to 200 characters")


class RecordDetail(BaseModel):
    id: UUID
    context_type: str | None
    severity: Literal["blocking", "advisory"]
    summary: str
    scope: list[str]
    scope_rows: list[ScopeEntry] = Field(description="Empty means the whole organization")
    is_locked: bool
    audience: str
    sources: list[RecordSource]
    confidence_score: float | None = Field(
        description="Evidence score: a weighted count of supporting events by source type "
        "that decays with age, not a probability"
    )
    created_at: datetime
    state: Literal["live", "retired"]
    retired_at: datetime | None
    retired_by: str | None
    retired_reason: str | None
    replaced_by: UUID | None
    supporting_events: SupportingEvents
    suggested_replacements: list[SuggestedReplacement] = Field(
        description="Open suggestions that another record replaces this one"
    )
    updates: list[RecordUpdate] = Field(
        default_factory=list, description="Up to 20 later events that agree with it, newest first"
    )
    may_be_replaced_by: ReplacementNote | None = Field(
        None, description="The newest live record suggested to replace it, while undecided"
    )


class RetireRequest(BaseModel):
    replaced_by: UUID | None = Field(None, description="The live record that replaces it")
    reason: str | None = Field(None, max_length=500)


class Suggestion(BaseModel):
    id: UUID
    basis: Literal["stated", "model"] = Field(
        description="stated: the newer record says it replaces an earlier decision; "
        "model: a model judged that it does"
    )
    similarity: float | None
    created_at: datetime
    record_id: UUID
    record_summary: str
    record_created_at: datetime
    replacement_id: UUID
    replacement_summary: str
    replacement_created_at: datetime


_LIFECYCLE_ERRORS = {
    "not_found": (404, "no such record"),
    "retired": (409, "the record is already retired"),
    "not_retired": (409, "the record is not retired"),
    "self": (422, "a record cannot replace itself"),
    "replacement_not_found": (422, "replaced_by is not a live record"),
}


def _lifecycle(error: str | None) -> None:
    if error is not None:
        status, detail = _LIFECYCLE_ERRORS[error]
        raise HTTPException(status_code=status, detail=detail)


@router.get("/records", tags=_REC, summary="List or search records")
async def list_records(
    repository: str | None = Query(None, description="Records scoped to this repository"),
    type: str | None = Query(None, description="Record type, e.g. adr"),
    kind: Literal["decision", "reference"] | None = Query(
        None,
        description=(
            "decision: types adr, constraint, business_requirement, risk, or blocking; "
            "reference: the rest"
        ),
    ),
    severity: Literal["blocking", "advisory"] | None = Query(None),
    state: Literal["live", "held", "retired"] = Query("live"),
    q: str | None = Query(None, max_length=200, description="Text in the summary"),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> Page[RecordSummary]:
    """Newest first, with redacted summaries."""
    rows, after = await records.search(
        db_engine,
        principal.tenant_id,
        principal.as_actor(),
        repository=repository,
        context_type=type,
        kind=kind,
        severity=severity,
        state=state,
        query=q,
        limit=limit,
        cursor=decode_cursor(cursor),
    )
    return Page(items=[RecordSummary(**r) for r in rows], next_cursor=encode_cursor(after))


@router.get("/records/suggestions", tags=_REC, summary="List suggested replacements")
async def list_suggestions(
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> Page[Suggestion]:
    """Open suggestions that a newer record replaces an older one. Nothing changes until one is
    accepted."""
    rows, after = await records.suggestions(
        db_engine,
        principal.tenant_id,
        principal.as_actor(),
        limit=limit,
        cursor=decode_cursor(cursor),
    )
    return Page(items=[Suggestion(**r) for r in rows], next_cursor=encode_cursor(after))


@router.post(
    "/records/suggestions/{suggestion_id}/{decision}",
    tags=_REC,
    summary="Accept or dismiss a suggested replacement",
)
async def decide_suggestion(
    suggestion_id: UUID,
    decision: Literal["accept", "dismiss"],
    request: Request,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, Any]:
    """Accept retires the older record, replaced by the newer. Dismiss keeps both."""
    _lifecycle(
        await records.decide_suggestion(
            db_engine,
            principal.tenant_id,
            suggestion_id,
            decision == "accept",
            principal.as_actor(),
            redis=_redis(request),
        )
    )
    return {"suggestion": str(suggestion_id), "decision": decision}


@router.post("/records/{record_id}/retire", tags=_REC, summary="Retire a record")
async def retire_record(
    record_id: UUID,
    body: RetireRequest,
    request: Request,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, Any]:
    """A retired record is no longer served to agents, written to context files or used by
    pull-request checks. Admins still see it and can restore it."""
    _lifecycle(
        await records.retire(
            db_engine,
            principal.tenant_id,
            record_id,
            principal.as_actor(),
            replaced_by=body.replaced_by,
            reason=body.reason,
            redis=_redis(request),
        )
    )
    return {"id": str(record_id), "state": "retired"}


@router.post("/records/{record_id}/restore", tags=_REC, summary="Restore a retired record")
async def restore_record(
    record_id: UUID,
    request: Request,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, Any]:
    _lifecycle(
        await records.restore(
            db_engine, principal.tenant_id, record_id, principal.as_actor(), redis=_redis(request)
        )
    )
    return {"id": str(record_id), "state": "live"}


@router.get("/records/{record_id}", tags=_REC, summary="Show a record")
async def show_record(
    record_id: UUID,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> RecordDetail:
    """The redacted summary with severity, scope, audience size, sources and lifecycle. Held
    records are shown under review instead."""
    found = await records.describe(db_engine, principal.tenant_id, record_id, principal.as_actor())
    if found is None:
        raise HTTPException(status_code=404, detail="no such record")
    return RecordDetail(**found)


@router.delete("/records/{record_id}", tags=_REC, summary="Delete a record")
async def delete_record(
    record_id: UUID,
    request: Request,
    dry_run: bool = Query(False),
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, Any]:
    """Deletes the record and the events linked only to it."""
    selection = purge.record(record_id)
    if not (await purge.count(db_engine, principal.tenant_id, selection)).records_deleted:
        raise HTTPException(status_code=404, detail="no such record")
    return await run_purge(request, principal, db_engine, selection, dry_run)


class HealthCheck(BaseModel):
    name: str
    status: Literal["PASS", "WARN", "FAIL"]
    detail: str


class Health(BaseModel):
    ok: bool
    checks: list[HealthCheck]


@router.get("/health", tags=_HEALTH, summary="Run the doctor checks")
async def health(
    request: Request, principal: Principal = Depends(require_admin_principal)
) -> Health:
    """The `checkcord doctor` checks. Counts cover only the caller's tenant."""
    oidc = getattr(request.app.state, "oidc", None)
    checks = await doctor.run_checks(get_settings(), principal.tenant_id, oidc)
    return Health(
        ok=all(c.ok for c in checks),
        checks=[HealthCheck(name=c.name, status=c.status, detail=c.detail) for c in checks],
    )
