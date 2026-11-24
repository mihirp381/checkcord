import re
from uuid import UUID

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.models.common import EntityReference
from checkcord.storage.models import JiraProjectKey

logger = structlog.get_logger(__name__)

# Compile Regex Patterns
GITHUB_PR_PATTERN = re.compile(
    r"https?://github\.com/([\w\-]+)/([\w\-]+)/pull/(\d+)", re.IGNORECASE
)
GITHUB_ISSUE_PATTERN = re.compile(
    r"https?://github\.com/([\w\-]+)/([\w\-]+)/issues/(\d+)", re.IGNORECASE
)
GITHUB_COMMIT_PATTERN = re.compile(
    r"https?://github\.com/([\w\-]+)/([\w\-]+)/commit/([a-f0-9]{7,40})", re.IGNORECASE
)
DATADOG_INCIDENT_PATTERN = re.compile(
    r"https?://app\.datadoghq\.(?:com|eu)/incidents/(\d+)", re.IGNORECASE
)
CONFLUENCE_PAGE_PATTERN = re.compile(
    r"https?://[\w\-]+\.atlassian\.net/wiki/spaces/([\w\-]+)/pages/(\d+)", re.IGNORECASE
)


async def extract_cross_links(
    content: str,
    tenant_id: UUID,
    db_engine: AsyncEngine,
) -> list[EntityReference]:
    """Scan content for external entity links and return a de-duplicated list of references."""
    if not content:
        return []

    refs: dict[tuple[str, str, str], EntityReference] = {}

    # Helper to add reference safely
    def add_ref(etype: str, eid: str, platform: str) -> None:
        key = (etype, eid, platform)
        if key not in refs:
            refs[key] = EntityReference(
                entity_type=etype,
                entity_id=eid,
                entity_platform=platform,
                relationship="mentions",
            )

    # Extract GitHub PRs, Issues, and Commits
    for match in GITHUB_PR_PATTERN.finditer(content):
        eid = f"{match.group(1)}/{match.group(2)}/pull/{match.group(3)}"
        add_ref("pr", eid, "github")

    for match in GITHUB_ISSUE_PATTERN.finditer(content):
        eid = f"{match.group(1)}/{match.group(2)}/issues/{match.group(3)}"
        add_ref("issue", eid, "github")

    for match in GITHUB_COMMIT_PATTERN.finditer(content):
        eid = f"{match.group(1)}/{match.group(2)}/commit/{match.group(3)}"
        add_ref("commit", eid, "github")

    # Extract Datadog Incidents
    for match in DATADOG_INCIDENT_PATTERN.finditer(content):
        add_ref("incident", match.group(1), "datadog")

    # Extract Confluence Wiki Pages
    for match in CONFLUENCE_PAGE_PATTERN.finditer(content):
        eid = f"{match.group(1)}/pages/{match.group(2)}"
        add_ref("wiki", eid, "confluence")

    # Extract Jira Ticket Keys using tenant-specific configured project keys
    try:
        async with db_engine.connect() as conn:
            # Bypass RLS to query the keys globally for the tenant
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            stmt = select(JiraProjectKey.project_key).where(JiraProjectKey.tenant_id == tenant_id)
            res = await conn.execute(stmt)
            project_keys = [row[0] for row in res.all()]

        if project_keys:
            # Escape keys and construct regex pattern
            escaped_keys = "|".join(re.escape(k) for k in project_keys)
            # Use word boundaries around the prefix and suffix to prevent partial matches
            jira_pattern = re.compile(rf"\b({escaped_keys})-(\d+)\b", re.IGNORECASE)
            for match in jira_pattern.finditer(content):
                ticket_id = f"{match.group(1).upper()}-{match.group(2)}"
                add_ref("ticket", ticket_id, "jira")
    except Exception as e:
        logger.exception(
            "failed_to_query_jira_project_keys", tenant_id=str(tenant_id), error=str(e)
        )

    return list(refs.values())
