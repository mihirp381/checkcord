import hashlib
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.chat.core.identity import resolve_author_identity
from checkcord.adapters.tasks.providers.jira.schemas import JiraWebhookPayload
from checkcord.models.common import AccessControl, AuthorInfo, EntityReference, TaskMetadata
from checkcord.models.enums import (
    AccessVisibility,
    AclType,
    EventAction,
    SourcePlatform,
    SourceType,
)
from checkcord.models.event import CanonicalEvent
from checkcord.storage.models import EnterpriseUser
from checkcord.storage.postgres import tenant_context

logger = structlog.get_logger(__name__).bind(component="jira_parser")


def adf_to_markdown(node: Any) -> str:
    """Recursively convert Atlassian Document Format (ADF) to clean Markdown."""
    if not node:
        return ""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type")
    if not node_type:
        return ""

    try:
        if node_type == "doc":
            return "".join(adf_to_markdown(child) for child in node.get("content", []))

        elif node_type == "paragraph":
            children_text = "".join(adf_to_markdown(child) for child in node.get("content", []))
            return children_text + "\n\n"

        elif node_type == "heading":
            attrs = node.get("attrs", {})
            level = attrs.get("level", 1)
            hashes = "#" * level
            children_text = "".join(adf_to_markdown(child) for child in node.get("content", []))
            return f"{hashes} {children_text}\n\n"

        elif node_type in ("bulletList", "orderedList"):
            items = node.get("content", [])
            lines = []
            for i, child in enumerate(items):
                prefix = "- " if node_type == "bulletList" else f"{i + 1}. "
                child_text = adf_to_markdown(child).strip()
                if child_text:
                    lines.append(f"{prefix}{child_text}")
            return "\n".join(lines) + "\n\n"

        elif node_type == "listItem":
            return "".join(adf_to_markdown(child) for child in node.get("content", []))

        elif node_type == "codeBlock":
            attrs = node.get("attrs", {})
            lang = attrs.get("language", "")
            children_text = "".join(adf_to_markdown(child) for child in node.get("content", []))
            return f"```{lang}\n{children_text}\n```\n\n"

        elif node_type == "table":
            rows = node.get("content", [])
            markdown_rows = []
            for i, row in enumerate(rows):
                row_cells = row.get("content", [])
                cell_texts = []
                for cell in row_cells:
                    cell_text = (
                        "".join(adf_to_markdown(c) for c in cell.get("content", []))
                        .strip()
                        .replace("\n", " ")
                    )
                    cell_texts.append(cell_text)
                markdown_rows.append("| " + " | ".join(cell_texts) + " |")
                if i == 0 and len(cell_texts) > 0:
                    markdown_rows.append("| " + " | ".join("---" for _ in cell_texts) + " |")
            return "\n".join(markdown_rows) + "\n\n"

        elif node_type == "text":
            text_val = str(node.get("text", ""))
            marks = node.get("marks", [])
            for mark in marks:
                mark_type = mark.get("type")
                if mark_type == "strong":
                    text_val = f"**{text_val}**"
                elif mark_type == "em":
                    text_val = f"*{text_val}*"
                elif mark_type == "strike":
                    text_val = f"~~{text_val}~~"
                elif mark_type == "underline":
                    text_val = f"<u>{text_val}</u>"
                elif mark_type == "code":
                    text_val = f"`{text_val}`"
                elif mark_type == "link":
                    attrs = mark.get("attrs", {})
                    url = attrs.get("href", "")
                    text_val = f"[{text_val}]({url})"
            return text_val

        elif node_type == "mention":
            attrs = node.get("attrs", {})
            text_val = str(attrs.get("text") or node.get("text") or f"@{attrs.get('id')}")
            return f"@{text_val.lstrip('@')}"

        elif node_type == "emoji":
            attrs = node.get("attrs", {})
            emoji_text = str(attrs.get("text") or attrs.get("shortName") or ":emoji:")
            return emoji_text

        elif node_type == "inlineCard":
            attrs = node.get("attrs", {})
            url = str(attrs.get("url", ""))
            return f"[{url}]({url})"

        elif node_type == "hardBreak":
            return "\n"

        elif node_type == "rule":
            return "---\n\n"

        elif node_type == "blockquote":
            children_text = "".join(adf_to_markdown(child) for child in node.get("content", []))
            lines = [f"> {line}" for line in children_text.strip().split("\n")]
            return "\n".join(lines) + "\n\n"

        if "content" in node:
            return "".join(adf_to_markdown(child) for child in node.get("content", []))

    except Exception as e:
        logger.warning("adf_parsing_error", error=str(e), node_type=node_type)
        return ""

    return ""


def normalize_content(content: dict[str, Any] | str | None) -> str:
    """Normalize plain text or ADF content to Markdown."""
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return adf_to_markdown(content).strip()
    return ""


def extract_entities(text: str) -> list[EntityReference]:
    """Extract mentioned entities (GitHub PRs, Jira Keys, Confluence docs) from text."""
    if not text:
        return []

    entities = []
    seen = set()

    # GitHub PR pattern: https://github.com/owner/repo/pull/num
    github_pr_pattern = re.compile(
        r"https?://(?:www\.)?github\.com/([a-zA-Z0-9_\-\.]+/[a-zA-Z0-9_\-\.]+)/pull/(\d+)"
    )
    for match in github_pr_pattern.finditer(text):
        url = match.group(0)
        repo = match.group(1)
        pr_num = match.group(2)
        entity_id = f"{repo}/pull/{pr_num}"
        if entity_id not in seen:
            seen.add(entity_id)
            entities.append(
                EntityReference(
                    entity_type="pr",
                    entity_id=entity_id,
                    entity_platform=SourcePlatform.GITHUB,
                    relationship="mention",
                )
            )

    # Jira ticket pattern: matches standard PROJECT-123
    jira_key_pattern = re.compile(r"\b([A-Z][A-Z0-9_]{1,9})-(\d+)\b")
    for match in jira_key_pattern.finditer(text):
        ticket_key = f"{match.group(1)}-{match.group(2)}"
        if ticket_key not in seen:
            seen.add(ticket_key)
            entities.append(
                EntityReference(
                    entity_type="ticket",
                    entity_id=ticket_key,
                    entity_platform=SourcePlatform.JIRA,
                    relationship="mention",
                )
            )

    # Confluence wiki page pattern: https://host/wiki/spaces/... or /wiki/pages/...
    confluence_pattern = re.compile(
        r"https?://[a-zA-Z0-9_\-\.]+\.atlassian\.net/wiki/(?:spaces/[a-zA-Z0-9_\-\.]+/pages/\d+|pages/\d+)[a-zA-Z0-9_\-\./\+]*"
    )
    for match in confluence_pattern.finditer(text):
        url = match.group(0).rstrip(".,;!?")
        if url not in seen:
            seen.add(url)
            entities.append(
                EntityReference(
                    entity_type="wiki_page",
                    entity_id=url,
                    entity_platform=SourcePlatform.CONFLUENCE,
                    relationship="mention",
                )
            )

    return entities


async def resolve_jira_user(
    db_engine: AsyncEngine,
    tenant_id: UUID,
    account_id: str | None,
    email: str | None,
) -> tuple[UUID | None, str | None, str | None]:
    """Resolve a Jira user's account ID or email to an enterprise user ID."""
    if account_id:
        internal_id, resolved_email, display_name = await resolve_author_identity(
            db_engine, tenant_id, "jira", account_id
        )
        if internal_id:
            return internal_id, resolved_email or email, display_name

    if email:
        async with tenant_context(db_engine, tenant_id) as conn:
            stmt = select(EnterpriseUser).where(
                EnterpriseUser.tenant_id == tenant_id,
                EnterpriseUser.primary_email == email,
            )
            res = await conn.execute(stmt)
            ent_user = res.mappings().fetchone()
            if ent_user:
                return ent_user["id"], email, ent_user["display_name"]

    return None, email, email


def jira_time(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        text = value.replace("Z", "+00:00")
        if len(text) > 5 and text[-5] in "+-" and text[-3] != ":":
            text = f"{text[:-2]}:{text[-2:]}"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return fallback
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return fallback


def _ms(when: datetime) -> str:
    return str(int(when.timestamp() * 1000))


def jira_source_id(issue_self: str, issue_id: str, *parts: str) -> str:
    """The one id a ticket, update or comment has, whether it came by webhook or by poll."""
    return ":".join(("jira", urlparse(issue_self).netloc, issue_id, *parts))


def _issue_meta(issue: dict[str, Any]) -> tuple[TaskMetadata, dict[str, Any]]:
    fields = issue.get("fields") or {}
    key = str(issue.get("key") or "")
    assignee = fields.get("assignee") or {}
    meta = TaskMetadata(
        status=(fields.get("status") or {}).get("name"),
        assignee_id=assignee.get("accountId"),
        project_key=key.split("-")[0] if "-" in key else None,
    )
    browse_url = f"{'/'.join(str(issue.get('self') or '').split('/')[:3])}/browse/{key}"
    return meta, {"html_url": browse_url, "issue_key": key}


async def jira_author(
    db_engine: AsyncEngine, tenant_id: UUID, user: dict[str, Any] | None
) -> AuthorInfo:
    if not user or not user.get("accountId"):
        return AuthorInfo(source_id="jira_system")
    internal_id, email, display_name = await resolve_jira_user(
        db_engine, tenant_id, user.get("accountId"), user.get("emailAddress")
    )
    return AuthorInfo(
        source_id=user["accountId"],
        internal_id=internal_id,
        email=email,
        display_name=display_name or user.get("displayName"),
    )


def _event(
    tenant_id: UUID,
    issue: dict[str, Any],
    *,
    source_type: SourceType,
    action: EventAction,
    source_id: str,
    revision: str,
    happened_at: datetime,
    author: AuthorInfo,
    text: str,
    raw: dict[str, Any],
    replaces: bool = False,
) -> CanonicalEvent:
    task_meta, metadata = _issue_meta(issue)
    if replaces:
        metadata["replaces"] = True
    return CanonicalEvent(
        event_id=uuid4(),
        tenant_id=tenant_id,
        source_platform=SourcePlatform.JIRA,
        source_type=source_type,
        event_action=action,
        source_id=source_id,
        fingerprint=hashlib.sha256(f"{source_id}:{revision}".encode()).hexdigest(),
        version=1,
        happened_at=happened_at,
        author=author,
        content_raw=text,
        content_clean=text,
        source_raw_payload=raw,
        access_control=AccessControl(
            is_public=False,
            visibility=AccessVisibility.PRIVATE,
            acl_type=AclType.PROJECT_ROLES,
            allowed_identity_ids=[],
        ),
        task_metadata=task_meta,
        entities=extract_entities(text),
        metadata=metadata,
    )


def ticket_created(
    tenant_id: UUID, issue: dict[str, Any], author: AuthorInfo, raw: dict[str, Any], at: datetime
) -> CanonicalEvent:
    fields = issue.get("fields") or {}
    text = (
        f"Ticket Created: {issue.get('key')} - {fields.get('summary') or ''}\n\n"
        f"{normalize_content(fields.get('description'))}"
    )
    return _event(
        tenant_id,
        issue,
        source_type=SourceType.TICKET_CREATED,
        action=EventAction.CREATED,
        source_id=jira_source_id(str(issue.get("self") or ""), str(issue["id"]), "created"),
        revision="",
        happened_at=jira_time(fields.get("created"), at),
        author=author,
        text=text,
        raw=raw,
    )


def ticket_updated(
    tenant_id: UUID,
    issue: dict[str, Any],
    author: AuthorInfo,
    raw: dict[str, Any],
    at: datetime,
    changes: str = "",
    status_change: bool = False,
) -> CanonicalEvent:
    fields = issue.get("fields") or {}
    when = jira_time(fields.get("updated"), at)
    text = (
        f"Ticket Updated: {issue.get('key')} - {fields.get('summary') or ''}\n\n"
        f"{changes}\n\n{normalize_content(fields.get('description'))}"
    ).strip()
    return _event(
        tenant_id,
        issue,
        source_type=SourceType.TASK if status_change else SourceType.TICKET_UPDATE,
        action=EventAction.UPDATED,
        source_id=jira_source_id(
            str(issue.get("self") or ""), str(issue["id"]), "updated", _ms(when)
        ),
        revision="",
        happened_at=when,
        author=author,
        text=text,
        raw=raw,
    )


def ticket_comment(
    tenant_id: UUID,
    issue: dict[str, Any],
    comment: dict[str, Any],
    author: AuthorInfo,
    raw: dict[str, Any],
    at: datetime,
) -> CanonicalEvent:
    created = jira_time(comment.get("created"), at)
    revised = jira_time(comment.get("updated"), created)
    name = (comment.get("author") or {}).get("displayName") or author.display_name or "someone"
    text = f"Comment on {issue.get('key')} by {name}:\n\n{normalize_content(comment.get('body'))}"
    return _event(
        tenant_id,
        issue,
        source_type=SourceType.TICKET_COMMENT,
        action=EventAction.UPDATED if revised > created else EventAction.CREATED,
        source_id=jira_source_id(
            str(issue.get("self") or ""), str(issue["id"]), "comment", str(comment["id"])
        ),
        revision=_ms(revised),
        happened_at=created,
        author=author,
        text=text,
        raw=raw,
        replaces=revised > created,
    )


_COMMENT_UPDATES = ("issue_commented", "issue_comment_edited", "issue_comment_deleted")


async def parse_jira_webhook_event(
    payload: JiraWebhookPayload,
    db_engine: AsyncEngine,
    tenant_id: UUID,
) -> CanonicalEvent | None:
    """Parse a Jira webhook payload into a CanonicalEvent."""
    event_type = payload.webhookEvent
    try:
        at = datetime.fromtimestamp(payload.timestamp / 1000.0, tz=UTC)
    except Exception:
        at = datetime.now(UTC)
    if payload.issue is None:
        return None
    raw = payload.model_dump(mode="json", by_alias=True)
    issue = raw["issue"]

    if event_type in ("comment_created", "comment_updated") and payload.comment:
        comment = raw["comment"]
        author = await jira_author(db_engine, tenant_id, comment.get("author"))
        return ticket_comment(tenant_id, issue, comment, author, raw, at)

    user = raw.get("user")
    if event_type == "jira:issue_created":
        return ticket_created(
            tenant_id, issue, await jira_author(db_engine, tenant_id, user), raw, at
        )
    if event_type == "jira:issue_updated":
        if payload.issue_event_type_name in _COMMENT_UPDATES:
            return None
        if not (issue.get("fields") or {}).get("updated"):
            issue["fields"]["updated"] = at.isoformat()
        items = payload.changelog.items if payload.changelog else []
        changes = "\n".join(
            f"Changed {i.field} from '{i.fromString or ''}' to '{i.toString or ''}'" for i in items
        )
        return ticket_updated(
            tenant_id,
            issue,
            await jira_author(db_engine, tenant_id, user),
            raw,
            at,
            changes=changes,
            status_change=any(i.field == "status" for i in items),
        )
    return None
