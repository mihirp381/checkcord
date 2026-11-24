"""Jira polling: each issue updated since the last poll, as the same events the webhook makes."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.ingestion import poll_cursor
from checkcord.adapters.jira_adapter import JiraAdapter, jira_base_url
from checkcord.adapters.tasks.providers.jira.parser import (
    jira_author,
    jira_time,
    ticket_comment,
    ticket_created,
    ticket_updated,
)
from checkcord.bus.interface import EventBus
from checkcord.core import http_retry
from checkcord.core.crypto import decrypt_secret
from checkcord.models.common import AuthorInfo
from checkcord.models.event import CanonicalEvent
from checkcord.services import scope
from checkcord.services.connections import connection_key
from checkcord.storage.models import JiraInstallation
from checkcord.storage.postgres import app_bypass_rls_context

logger = structlog.get_logger(__name__).bind(component="jira_document_poller")


PAGE_SIZE = 50


def _updated(issue: dict[str, Any]) -> datetime:
    value = str(issue["fields"]["updated"])
    if len(value) > 5 and value[-5] in "+-" and value[-3] != ":":
        value = f"{value[:-2]}:{value[-2:]}"
    return poll_cursor.parse(value)


async def _issues_after(
    adapter: JiraAdapter, jira_host: str, since: datetime
) -> tuple[list[dict[str, Any]], bool]:
    """The oldest issues updated after `since`, oldest first, and whether more follow."""
    client = await adapter._get_client()
    url = f"{jira_base_url(jira_host)}/rest/api/3/search"
    minutes = max(1, math.ceil((datetime.now(UTC) - since).total_seconds() / 60) + 1)
    start_at = 0
    while True:
        response = await http_retry.send(
            "jira",
            client,
            "GET",
            url,
            params={
                "jql": f'updated >= "-{minutes}m" ORDER BY updated ASC, key ASC',
                "startAt": start_at,
                "maxResults": PAGE_SIZE,
                "fields": "updated",
            },
        )
        response.raise_for_status()
        data = response.json()
        page: list[dict[str, Any]] = data.get("issues", [])
        fresh = sorted((i for i in page if _updated(i) > since), key=_updated)
        more = start_at + len(page) < int(data.get("total", 0))
        if fresh or not page or not more:
            return fresh, more
        start_at += len(page)


async def issue_events(
    db_engine: AsyncEngine, tenant_id: UUID, issue: dict[str, Any]
) -> list[CanonicalEvent]:
    """The ticket and each comment as their own events, each under its own author."""
    fields = issue.get("fields") or {}
    now = datetime.now(UTC)
    raw = {"issue": {k: v for k, v in issue.items() if k != "fields"}}
    reporter = fields.get("reporter") or fields.get("creator")
    author = await jira_author(db_engine, tenant_id, reporter)
    events = [ticket_created(tenant_id, issue, author, raw, now)]
    comments = (fields.get("comment") or {}).get("comments") or []
    created = jira_time(fields.get("created"), now)
    updated = jira_time(fields.get("updated"), created)
    touched = {jira_time(c.get(f), now) for c in comments for f in ("created", "updated")}
    if updated - created > timedelta(seconds=1) and updated not in touched:
        system = AuthorInfo(source_id="jira_system")
        events.append(ticket_updated(tenant_id, issue, system, raw, now))
    for comment in comments:
        author = await jira_author(db_engine, tenant_id, comment.get("author"))
        events.append(ticket_comment(tenant_id, issue, comment, author, raw, now))
    return events


async def _poll_installation(db_engine: AsyncEngine, bus: EventBus, row: Any, log: Any) -> int:
    adapter = JiraAdapter(
        jira_host=row.jira_host, personal_access_token=decrypt_secret(row.personal_access_token)
    )
    key = connection_key("jira", row.jira_host)
    cursor = poll_cursor.start(row.poll_cursor)
    published = 0
    try:
        for _ in range(poll_cursor.MAX_PAGES):
            issues, more = await _issues_after(adapter, row.jira_host, cursor)
            for issue in issues:
                issue_key = issue["key"]
                project = issue_key.partition("-")[0]
                if await scope.allows(db_engine, row.tenant_id, key, project, count=False):
                    issue_payload = await adapter.fetch_issue(issue_key)
                    for event in await issue_events(db_engine, row.tenant_id, issue_payload):
                        event = event.model_copy(
                            update={
                                "connection": key,
                                "audience_container": scope.normalize(project),
                            }
                        )
                        await bus.publish(topic="events.raw.tasks", event=event)
                        published += 1
                cursor = _updated(issue)
            if not more or not issues:
                return published
        log.warning("jira_poll_page_cap_reached", pages=poll_cursor.MAX_PAGES)
        return published
    finally:
        if cursor != row.poll_cursor:
            await poll_cursor.save(db_engine, JiraInstallation, row.id, poll_cursor=cursor)


async def poll_jira_documents(db_engine: AsyncEngine, bus: EventBus) -> int:
    """Scheduler entrypoint: publish every issue updated since each installation's last poll,
    parsed by the full ADF `JiraAdapter`.

    Returns the number of documents published.
    """
    async with app_bypass_rls_context(db_engine) as conn:
        res = await conn.execute(
            select(
                JiraInstallation.id,
                JiraInstallation.jira_host,
                JiraInstallation.tenant_id,
                JiraInstallation.personal_access_token,
                JiraInstallation.poll_cursor,
            )
        )
        installations = res.fetchall()

    published = 0
    for row in installations:
        log = logger.bind(tenant_id=str(row.tenant_id), jira_host=row.jira_host)
        try:
            published += await _poll_installation(db_engine, bus, row, log)
        except Exception as e:
            log.exception("jira_document_poll_failed_for_installation", error=str(e))

    log_summary = logger.bind(published=published, installations=len(installations))
    log_summary.info("jira_document_poll_completed")
    return published
