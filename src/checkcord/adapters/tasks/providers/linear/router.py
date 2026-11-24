"""Linear OAuth install and issue polling."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode
from uuid import uuid4

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.ingestion import poll_cursor
from checkcord.adapters.ingestion.document_to_event import document_to_canonical_event
from checkcord.adapters.linear_adapter import LinearAdapter
from checkcord.bus.interface import EventBus
from checkcord.connectors import install_page
from checkcord.core import http_retry
from checkcord.core.config import get_settings
from checkcord.core.crypto import decrypt_secret, encrypt_secret
from checkcord.core.oauth_state import InvalidStateError, consume_state, read_state
from checkcord.models.common import AuthorInfo
from checkcord.models.event import CanonicalEvent
from checkcord.services import scope
from checkcord.services.connections import connection_key
from checkcord.storage import audit
from checkcord.storage.models import LinearInstallation
from checkcord.storage.postgres import app_bypass_rls_context

logger = structlog.get_logger(__name__).bind(component="linear_router")

router = APIRouter()

_ORGANIZATION_QUERY = "query { viewer { organization { id } } }"
PAGE_SIZE = 50
_COMMENTS = """
            nodes {
                id
                body
                createdAt
                updatedAt
                user { name email }
            }
            pageInfo { hasNextPage endCursor }
"""
_ISSUES_QUERY = (
    """
query GetIssues(
    $first: Int, $last: Int, $after: String, $before: String, $since: DateTimeOrDuration!
) {
    issues(
        first: $first, last: $last, after: $after, before: $before, orderBy: updatedAt,
        filter: { updatedAt: { gt: $since } }
    ) {
        nodes {
            id
            identifier
            title
            updatedAt
            creator { name email }
            team { id key }
            description
            priority
            state { name }
            comments(first: 50) {"""
    + _COMMENTS
    + """            }
        }
        pageInfo { hasNextPage hasPreviousPage startCursor endCursor }
    }
}
"""
)
_COMMENTS_QUERY = (
    """
query GetComments($id: String!, $after: String) {
    issue(id: $id) {
        comments(first: 50, after: $after) {"""
    + _COMMENTS
    + """        }
    }
}
"""
)


def _api(path: str) -> str:
    return f"{get_settings().linear_api_url}/{path}"


@router.get("/api/v1/webhooks/linear/install")
async def linear_install(state: str | None = Query(None)) -> RedirectResponse:
    """Redirect to Linear's OAuth page from an install link minted by `checkcord connect linear`."""
    settings = get_settings()
    if not settings.linear_client_id or not settings.linear_redirect_uri:
        raise HTTPException(status_code=500, detail="Linear OAuth is not configured")
    try:
        read_state(state, "linear")
    except InvalidStateError as err:
        raise HTTPException(status_code=400, detail=f"Invalid install link: {err}") from err

    params = {
        "client_id": settings.linear_client_id,
        "redirect_uri": settings.linear_redirect_uri,
        "response_type": "code",
        "scope": "read",
        "state": state,
    }
    authorize = f"{settings.linear_oauth_url}/oauth/authorize"
    return RedirectResponse(url=f"{authorize}?{urlencode(params, quote_via=quote)}")


@router.get("/api/v1/webhooks/linear/oauth_redirect", response_class=HTMLResponse)
async def linear_oauth_redirect(
    request: Request, code: str | None = Query(None), state: str | None = Query(None)
) -> HTMLResponse:
    """Linear's OAuth redirect; shows the install page."""
    try:
        await _install(request, code, state)
    except HTTPException as err:
        return install_page.failed("Linear", str(err.detail), err.status_code)
    return install_page.connected("Linear")


async def _install(request: Request, code: str | None, state: str | None) -> None:
    """Exchange the code for a token and store the installation."""
    settings = get_settings()
    if not settings.linear_client_id or not settings.linear_client_secret:
        raise HTTPException(status_code=500, detail="Linear OAuth is not configured")

    try:
        redis = request.app.state.ctx.redis_client
        tenant_id = (await consume_state(redis, state, "linear")).tenant_id
    except InvalidStateError as err:
        logger.warning("linear_oauth_state_rejected", reason=str(err))
        raise HTTPException(status_code=400, detail=f"Invalid install link: {err}") from err
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await http_retry.send(
            "linear",
            client,
            "POST",
            _api("oauth/token"),
            data={
                "client_id": settings.linear_client_id,
                "client_secret": settings.linear_client_secret,
                "code": code,
                "redirect_uri": settings.linear_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if response.status_code != 200:
        logger.error("linear_oauth_token_exchange_failed", status_code=response.status_code)
        raise HTTPException(status_code=400, detail="Linear did not accept the authorization code")

    token_data = response.json()
    access_token = token_data["access_token"]
    organization_id = await _fetch_organization_id(access_token)

    db_engine: AsyncEngine = request.app.state.ctx.db_engine
    encrypted_token = encrypt_secret(access_token)

    async with app_bypass_rls_context(db_engine) as conn:
        stmt = pg_insert(LinearInstallation).values(
            linear_organization_id=organization_id,
            tenant_id=tenant_id,
            access_token=encrypted_token,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[LinearInstallation.linear_organization_id],
            set_={"access_token": stmt.excluded.access_token, "updated_at": func.now()},
            where=LinearInstallation.tenant_id == stmt.excluded.tenant_id,
        ).returning(LinearInstallation.id)
        saved = (await conn.execute(stmt)).scalar_one_or_none()
        if saved is not None:
            await audit.record(
                conn,
                tenant_id,
                audit.Actor(channel=audit.API),
                "connection.added",
                params={"provider": "linear", "identifier": organization_id, "via": "oauth"},
            )
    if saved is None:
        raise HTTPException(status_code=409, detail="Already connected to a different tenant")

    logger.info(
        "linear_installation_saved", organization_id=organization_id, tenant_id=str(tenant_id)
    )


async def _fetch_organization_id(access_token: str) -> str:
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        timeout=30.0,
    ) as client:
        response = await http_retry.send(
            "linear", client, "POST", _api("graphql"), json={"query": _ORGANIZATION_QUERY}
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        org_id: str = data["data"]["viewer"]["organization"]["id"]
        return org_id


async def _graphql(client: httpx.AsyncClient, query: str, **variables: Any) -> dict[str, Any]:
    response = await http_retry.send(
        "linear", client, "POST", _api("graphql"), json={"query": query, "variables": variables}
    )
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    if body.get("errors"):
        raise RuntimeError(f"Linear answered with errors: {body['errors']}")
    data: dict[str, Any] = body["data"]
    return data


def _updated(issue: dict[str, Any]) -> datetime:
    return poll_cursor.parse(str(issue["updatedAt"]))


@dataclass(frozen=True)
class _Next:
    """Where the next, newer page starts: before ``token`` when the provider sorts newest
    first (Linear does), after it when it sorts oldest first."""

    newest_first: bool
    token: str | None


async def _issues_page(
    client: httpx.AsyncClient, since: datetime, page: _Next | None = None
) -> tuple[list[dict[str, Any]], _Next | None]:
    """One page of issues updated after ``since``, oldest first, and where the next one starts.

    The first page is the oldest; each next page follows the previous one's Relay cursor.
    """
    window = {"since": since.isoformat()}
    if page is None:
        data = (await _graphql(client, _ISSUES_QUERY, last=PAGE_SIZE, **window))["issues"]
        nodes, info = data["nodes"], data["pageInfo"]
        newest_first = not (len(nodes) > 1 and _updated(nodes[0]) < _updated(nodes[-1]))
        if not newest_first and info.get("hasPreviousPage"):
            data = (await _graphql(client, _ISSUES_QUERY, first=PAGE_SIZE, **window))["issues"]
            nodes, info = data["nodes"], data["pageInfo"]
    elif page.newest_first:
        data = (await _graphql(client, _ISSUES_QUERY, last=PAGE_SIZE, before=page.token, **window))[
            "issues"
        ]
        nodes, info, newest_first = data["nodes"], data["pageInfo"], True
    else:
        data = (await _graphql(client, _ISSUES_QUERY, first=PAGE_SIZE, after=page.token, **window))[
            "issues"
        ]
        nodes, info, newest_first = data["nodes"], data["pageInfo"], False
    if newest_first:
        more, token = info.get("hasPreviousPage"), info.get("startCursor")
    else:
        more, token = info.get("hasNextPage"), info.get("endCursor")
    following = _Next(newest_first, token) if more and token and nodes else None
    return sorted(nodes, key=_updated), following


async def _all_comments(client: httpx.AsyncClient, issue: dict[str, Any]) -> None:
    comments = issue.setdefault("comments", {"nodes": []})
    info = comments.get("pageInfo") or {}
    while info.get("hasNextPage"):
        page = (
            await _graphql(client, _COMMENTS_QUERY, id=issue["id"], after=info.get("endCursor"))
        )["issue"]["comments"]
        comments["nodes"].extend(page["nodes"])
        info = page["pageInfo"]


def issue_events(
    adapter: LinearAdapter, tenant_id: Any, issue: dict[str, Any], key: str, container: str | None
) -> list[CanonicalEvent]:
    """The issue under its creator, and each comment as its own event under its author."""
    comments = (issue.get("comments") or {}).get("nodes") or []
    document = adapter.parse_issue_to_document({**issue, "comments": {"nodes": []}})
    creator = issue.get("creator") or {}
    ticket = document_to_canonical_event(
        document, tenant_id, connection=key, container=container
    ).model_copy(update={"author": _author(creator)})
    events = [ticket]
    identifier = issue.get("identifier") or document.source_id
    for comment in comments:
        user = comment.get("user") or {}
        name = user.get("name") or "someone"
        text = f"Comment on {identifier} by {name}:\n\n{comment.get('body') or ''}"
        source_id = f"{identifier}:comment:{comment['id']}"
        revised = str(comment.get("updatedAt") or comment.get("body") or "")
        events.append(
            ticket.model_copy(
                update={
                    "event_id": uuid4(),
                    "source_id": source_id,
                    "fingerprint": hashlib.sha256(f"{source_id}:{revised}".encode()).hexdigest(),
                    "happened_at": poll_cursor.parse(str(comment["createdAt"]))
                    if comment.get("createdAt")
                    else ticket.happened_at,
                    "author": _author(user),
                    "content_raw": text,
                    "content_clean": text,
                    "source_raw_payload": {"comment_id": comment["id"], "issue": identifier},
                    "metadata": {"replaces": True},
                }
            )
        )
    return events


def _author(user: dict[str, Any]) -> AuthorInfo:
    if not user:
        return AuthorInfo(source_id="unknown", display_name="Unknown Author")
    return AuthorInfo(
        source_id=user.get("email") or user.get("name") or "unknown",
        email=user.get("email"),
        display_name=user.get("name"),
    )


async def _poll_organization(db_engine: AsyncEngine, bus: EventBus, row: Any, log: Any) -> int:
    access_token = decrypt_secret(row.access_token)
    adapter = LinearAdapter(api_key=f"Bearer {access_token}")
    key = connection_key("linear", row.linear_organization_id)
    cursor = poll_cursor.start(row.poll_cursor)
    published = 0
    try:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            timeout=30.0,
        ) as client:
            page: _Next | None = None
            since = cursor
            for _ in range(poll_cursor.MAX_PAGES):
                issues, page = await _issues_page(client, since, page)
                for issue_data in issues:
                    team = issue_data.get("team") or {}
                    if await scope.allows(
                        db_engine,
                        row.tenant_id,
                        key,
                        team.get("id"),
                        team.get("key")
                        or str(issue_data.get("identifier") or "").partition("-")[0],
                        count=False,
                    ):
                        await _all_comments(client, issue_data)
                        for event in issue_events(
                            adapter,
                            row.tenant_id,
                            issue_data,
                            key,
                            team.get("key") or team.get("id"),
                        ):
                            await bus.publish(topic="events.raw.tasks", event=event)
                            published += 1
                    cursor = _updated(issue_data)
                if page is None:
                    return published
            log.warning("linear_poll_page_cap_reached", pages=poll_cursor.MAX_PAGES)
            return published
    finally:
        if cursor != row.poll_cursor:
            await poll_cursor.save(db_engine, LinearInstallation, row.id, poll_cursor=cursor)


async def poll_linear_organizations(db_engine: AsyncEngine, bus: EventBus) -> int:
    """Scheduler entrypoint: publish every issue updated since each organization's last poll.

    Returns the number of documents published.
    """
    async with app_bypass_rls_context(db_engine) as conn:
        res = await conn.execute(
            select(
                LinearInstallation.id,
                LinearInstallation.linear_organization_id,
                LinearInstallation.tenant_id,
                LinearInstallation.access_token,
                LinearInstallation.poll_cursor,
            )
        )
        installations = res.fetchall()

    published = 0
    for row in installations:
        log = logger.bind(tenant_id=str(row.tenant_id), organization_id=row.linear_organization_id)
        try:
            published += await _poll_organization(db_engine, bus, row, log)
        except Exception as e:
            log.exception("linear_poll_failed_for_organization", error=str(e))

    log_summary = logger.bind(published=published, organizations=len(installations))
    log_summary.info("linear_poll_completed")
    return published
