"""Notion OAuth install and page polling."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.ingestion import poll_cursor
from checkcord.adapters.ingestion.document_to_event import document_to_canonical_event
from checkcord.adapters.notion_adapter import NotionAdapter
from checkcord.bus.interface import EventBus
from checkcord.connectors import install_page
from checkcord.core import http_retry
from checkcord.core.config import get_settings
from checkcord.core.crypto import decrypt_secret, encrypt_secret
from checkcord.core.oauth_state import InvalidStateError, consume_state, read_state
from checkcord.models.common import AuthorInfo
from checkcord.services import scope
from checkcord.services.connections import connection_key
from checkcord.storage import audit
from checkcord.storage.models import NotionInstallation
from checkcord.storage.postgres import app_bypass_rls_context

logger = structlog.get_logger(__name__).bind(component="notion_router")

router = APIRouter()

PAGE_SIZE = 100
# Notion rounds last_edited_time to the minute, so a page is read only once its minute has closed.
SETTLE = timedelta(minutes=2)


def _api(path: str) -> str:
    return f"{get_settings().notion_api_url}/v1/{path}"


@router.get("/api/v1/webhooks/notion/install")
async def notion_install(state: str | None = Query(None)) -> RedirectResponse:
    """Redirect to Notion's OAuth page from an install link minted by `checkcord connect notion`."""
    settings = get_settings()
    if not settings.notion_client_id or not settings.notion_redirect_uri:
        raise HTTPException(status_code=500, detail="Notion OAuth is not configured")
    try:
        read_state(state, "notion")
    except InvalidStateError as err:
        raise HTTPException(status_code=400, detail=f"Invalid install link: {err}") from err

    params = {
        "client_id": settings.notion_client_id,
        "redirect_uri": settings.notion_redirect_uri,
        "response_type": "code",
        "owner": "user",
        "state": state,
    }
    return RedirectResponse(url=f"{_api('oauth/authorize')}?{urlencode(params, quote_via=quote)}")


@router.get("/api/v1/webhooks/notion/oauth_redirect", response_class=HTMLResponse)
async def notion_oauth_redirect(
    request: Request, code: str | None = Query(None), state: str | None = Query(None)
) -> HTMLResponse:
    """Notion's OAuth redirect; shows the install page."""
    try:
        await _install(request, code, state)
    except HTTPException as err:
        return install_page.failed("Notion", str(err.detail), err.status_code)
    return install_page.connected("Notion")


async def _install(request: Request, code: str | None, state: str | None) -> None:
    """Exchange the code for a token and store the installation."""
    settings = get_settings()
    if not settings.notion_client_id or not settings.notion_client_secret:
        raise HTTPException(status_code=500, detail="Notion OAuth is not configured")

    try:
        redis = request.app.state.ctx.redis_client
        tenant_id = (await consume_state(redis, state, "notion")).tenant_id
    except InvalidStateError as err:
        logger.warning("notion_oauth_state_rejected", reason=str(err))
        raise HTTPException(status_code=400, detail=f"Invalid install link: {err}") from err
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    basic_auth = base64.b64encode(
        f"{settings.notion_client_id}:{settings.notion_client_secret}".encode()
    ).decode()

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await http_retry.send(
            "notion",
            client,
            "POST",
            _api("oauth/token"),
            headers={
                "Authorization": f"Basic {basic_auth}",
                "Content-Type": "application/json",
            },
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.notion_redirect_uri,
            },
        )
    if response.status_code != 200:
        logger.error("notion_oauth_token_exchange_failed", status_code=response.status_code)
        raise HTTPException(status_code=400, detail="Notion did not accept the authorization code")

    token_data = response.json()
    access_token = token_data["access_token"]
    workspace_id = token_data.get("workspace_id") or token_data.get("bot_id")

    db_engine: AsyncEngine = request.app.state.ctx.db_engine
    encrypted_token = encrypt_secret(access_token)

    async with app_bypass_rls_context(db_engine) as conn:
        stmt = pg_insert(NotionInstallation).values(
            notion_workspace_id=workspace_id,
            tenant_id=tenant_id,
            access_token=encrypted_token,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[NotionInstallation.notion_workspace_id],
            set_={"access_token": stmt.excluded.access_token, "updated_at": func.now()},
            where=NotionInstallation.tenant_id == stmt.excluded.tenant_id,
        ).returning(NotionInstallation.id)
        saved = (await conn.execute(stmt)).scalar_one_or_none()
        if saved is not None:
            await audit.record(
                conn,
                tenant_id,
                audit.Actor(channel=audit.API),
                "connection.added",
                params={"provider": "notion", "identifier": str(workspace_id), "via": "oauth"},
            )
    if saved is None:
        raise HTTPException(status_code=409, detail="Already connected to a different tenant")

    logger.info("notion_installation_saved", workspace_id=workspace_id, tenant_id=str(tenant_id))


async def _search_page(
    access_token: str, start_cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """One page of the Search API, least recently edited first, and the cursor of the next."""
    body: dict[str, Any] = {
        "filter": {"value": "page", "property": "object"},
        "sort": {"timestamp": "last_edited_time", "direction": "ascending"},
        "page_size": PAGE_SIZE,
    }
    if start_cursor:
        body["start_cursor"] = start_cursor
    async with httpx.AsyncClient(
        headers={
            "Authorization": f"Bearer {access_token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    ) as client:
        response = await http_retry.send("notion", client, "POST", _api("search"), json=body)
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        following = data.get("next_cursor") if data.get("has_more") else None
        return list(data.get("results", [])), following


def _page_title(page: dict[str, Any]) -> str:
    properties = page.get("properties", {})
    for prop in properties.values():
        if prop.get("type") == "title":
            title_parts = prop.get("title", [])
            return "".join(t.get("plain_text", "") for t in title_parts) or "Untitled"
    return "Untitled"


async def _publish_page(
    db_engine: AsyncEngine,
    bus: EventBus,
    adapter: NotionAdapter,
    row: Any,
    key: str,
    page: dict[str, Any],
) -> bool:
    page_id = page["id"]
    parent = page.get("parent") or {}
    if not await scope.allows(
        db_engine,
        row.tenant_id,
        key,
        page_id,
        parent.get("database_id") or parent.get("page_id"),
        count=False,
    ):
        return False
    blocks = await adapter.fetch_page_blocks(page_id)
    document = adapter.parse_page_to_document(
        page_id=page_id, title=_page_title(page), raw_blocks=blocks
    )
    event = document_to_canonical_event(
        document,
        row.tenant_id,
        url=page.get("url", ""),
        connection=key,
        container=parent.get("database_id") or parent.get("page_id") or page_id,
    )
    creator = (page.get("created_by") or {}).get("id")
    if creator:
        event = event.model_copy(update={"author": AuthorInfo(source_id=creator)})
    await bus.publish(topic="events.raw.tasks", event=event)
    return True


async def _poll_workspace(db_engine: AsyncEngine, bus: EventBus, row: Any, log: Any) -> int:
    access_token = decrypt_secret(row.access_token)
    adapter = NotionAdapter(access_token=access_token)
    key = connection_key("notion", row.notion_workspace_id)
    cursor = poll_cursor.start(row.poll_cursor)
    resume: str | None = row.poll_resume
    settled = datetime.now(UTC) - SETTLE
    published = 0
    try:
        for _ in range(poll_cursor.MAX_PAGES):
            try:
                pages, following = await _search_page(access_token, resume)
            except httpx.HTTPStatusError as err:
                if resume is None or err.response.status_code != 400:
                    raise
                resume = None
                continue
            for page in pages:
                edited = poll_cursor.parse(str(page["last_edited_time"]))
                if edited > settled:
                    resume = None
                    return published
                if edited <= cursor:
                    continue
                if await _publish_page(db_engine, bus, adapter, row, key, page):
                    published += 1
                cursor = edited
            resume = following
            if resume is None:
                return published
        log.warning("notion_poll_page_cap_reached", pages=poll_cursor.MAX_PAGES)
        return published
    finally:
        if cursor != row.poll_cursor or resume != row.poll_resume:
            await poll_cursor.save(
                db_engine, NotionInstallation, row.id, poll_cursor=cursor, poll_resume=resume
            )


async def poll_notion_workspaces(db_engine: AsyncEngine, bus: EventBus) -> int:
    """Scheduler entrypoint: publish every page edited since each workspace's last poll.

    Returns the number of documents published.
    """
    async with app_bypass_rls_context(db_engine) as conn:
        res = await conn.execute(
            select(
                NotionInstallation.id,
                NotionInstallation.notion_workspace_id,
                NotionInstallation.tenant_id,
                NotionInstallation.access_token,
                NotionInstallation.poll_cursor,
                NotionInstallation.poll_resume,
            )
        )
        installations = res.fetchall()

    published = 0
    for row in installations:
        log = logger.bind(tenant_id=str(row.tenant_id), workspace_id=row.notion_workspace_id)
        try:
            published += await _poll_workspace(db_engine, bus, row, log)
        except Exception as e:
            log.exception("notion_poll_failed_for_workspace", error=str(e))

    log_summary = logger.bind(published=published, workspaces=len(installations))
    log_summary.info("notion_poll_completed")
    return published
