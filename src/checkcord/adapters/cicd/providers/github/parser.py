from typing import Any
from uuid import UUID, uuid4

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.cicd.providers.github.schemas import GitHubWorkflowRunPayload
from checkcord.adapters.code.providers.github.parser import get_repository_acl
from checkcord.models.common import AuthorInfo, EntityReference
from checkcord.models.enums import EventAction, SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent

logger = structlog.get_logger(__name__)


async def parse_workflow_run_event(
    payload: GitHubWorkflowRunPayload,
    db_engine: AsyncEngine,
    redis_client: Redis | None,
    tenant_id: UUID,
    pat: str,
    failure_content: str | None = None,
    extracted_failure_summaries: list[str] | None = None,
    event_id: UUID | None = None,
) -> CanonicalEvent | None:
    """Parse a GitHub workflow run event into a CanonicalEvent if it failed.

    ``failure_content`` (already redacted by the caller) and
    ``extracted_failure_summaries`` are optional and add the failing-step
    content to the event.
    """
    run = payload.workflow_run
    if run.status != "completed" or run.conclusion != "failure":
        logger.info("ignored_workflow_run_event", status=run.status, conclusion=run.conclusion)
        return None

    acl = await get_repository_acl(
        db_engine,
        redis_client,
        tenant_id,
        payload.repository.full_name,
        payload.repository.private,
        pat,
    )

    author = AuthorInfo(
        source_id="github_actions",
        display_name="GitHub Actions",
    )

    content = (
        f"# GitHub Action CI/CD Failure: {run.name}\n\n"
        f"**Repository:** {payload.repository.full_name}\n"
        f"**Branch:** {run.head_branch}\n"
        f"**Commit SHA:** {run.head_sha}\n"
        f"**Status:** {run.status}\n"
        f"**Conclusion:** {run.conclusion}\n\n"
        f"**Workflow Run URL:** {run.html_url}"
    )
    if failure_content:
        content += f"\n\n## Failure Content\n\n{failure_content}"

    entities = [
        EntityReference(
            entity_type="repo",
            entity_id=payload.repository.full_name,
            entity_platform="github",
            relationship="affected_repository",
        )
    ]

    resolved_event_id = event_id if event_id is not None else uuid4()
    source_id = f"github:workflow_run:{run.id}"

    metadata: dict[str, Any] = {
        "repository": payload.repository.full_name,
        "branch": run.head_branch,
        "html_url": run.html_url,
        "commit_sha": run.head_sha,
    }
    if extracted_failure_summaries:
        metadata["extracted_failure_summaries"] = extracted_failure_summaries

    return CanonicalEvent(
        event_id=resolved_event_id,
        tenant_id=tenant_id,
        source_platform=SourcePlatform.GITHUB,
        source_type=SourceType.CI_PIPELINE,
        event_action=EventAction.CREATED,
        source_id=source_id,
        version=1,
        happened_at=run.updated_at,
        author=author,
        content_raw=content,
        content_clean=content,
        source_raw_payload=payload.model_dump(mode="json"),
        access_control=acl,
        audience_container=payload.repository.full_name,
        entities=entities,
        metadata=metadata,
    )
