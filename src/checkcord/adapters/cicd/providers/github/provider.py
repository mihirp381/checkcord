"""CI runs and failure details from GitHub."""

from typing import Any
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.cicd.providers.github.parser import parse_workflow_run_event
from checkcord.adapters.cicd.providers.github.schemas import GitHubWorkflowRunPayload
from checkcord.adapters.cicd.providers.github.workflow_parser import parse_workflow_yaml
from checkcord.adapters.code.providers.github.rest_client import GitHubRestClient
from checkcord.core.exceptions import GitHubAPIError
from checkcord.models.cicd import CIFailureAnnotation, CIFailureContent, WorkflowDefinition
from checkcord.models.event import CanonicalEvent

logger = structlog.get_logger(__name__).bind(component="github_cicd_provider")


class GitHubCICDProvider:
    """CI runs from GitHub's REST, Checks and Actions APIs."""

    async def parse_run_event(
        self,
        payload: Any,
        db_engine: AsyncEngine,
        redis_client: Redis | None,
        tenant_id: UUID,
        pat: str,
        failure_content: str | None = None,
        extracted_failure_summaries: list[str] | None = None,
        event_id: UUID | None = None,
    ) -> CanonicalEvent | None:
        typed_payload: GitHubWorkflowRunPayload = (
            payload
            if isinstance(payload, GitHubWorkflowRunPayload)
            else GitHubWorkflowRunPayload.model_validate(payload)
        )
        return await parse_workflow_run_event(
            typed_payload,
            db_engine,
            redis_client,
            tenant_id,
            pat,
            failure_content=failure_content,
            extracted_failure_summaries=extracted_failure_summaries,
            event_id=event_id,
        )

    async def fetch_workflow_definition(
        self, owner: str, repo: str, workflow_path: str, ref: str, pat: str
    ) -> WorkflowDefinition | None:
        client = GitHubRestClient(pat)
        content = await client.get_file_content(owner, repo, workflow_path, ref)
        if content is None:
            return None
        return parse_workflow_yaml(workflow_path, content)

    async def fetch_failure_content(
        self,
        owner: str,
        repo: str,
        ref: str,
        run_id: int,
        pat: str,
        max_log_lines: int = 500,
    ) -> CIFailureContent:
        """Check annotations first; bounded job-log excerpt as fallback."""
        client = GitHubRestClient(pat)

        annotations: list[CIFailureAnnotation] = []
        try:
            check_runs = await client.list_check_runs_for_ref(owner, repo, ref)
        except GitHubAPIError as e:
            logger.warning("cicd_provider.check_runs_fetch_failed", ref=ref, error=str(e))
            check_runs = []

        for run in check_runs:
            if run.get("conclusion") != "failure":
                continue
            try:
                raw_annotations = await client.get_check_run_annotations(owner, repo, run["id"])
            except GitHubAPIError as e:
                logger.warning(
                    "cicd_provider.annotations_fetch_failed",
                    check_run_id=run.get("id"),
                    error=str(e),
                )
                continue
            for annotation in raw_annotations:
                annotations.append(
                    CIFailureAnnotation(
                        path=annotation.get("path"),
                        start_line=annotation.get("start_line"),
                        end_line=annotation.get("end_line"),
                        message=annotation.get("message") or "",
                        annotation_level=annotation.get("annotation_level"),
                    )
                )

        if annotations:
            return CIFailureContent(source="annotations", annotations=annotations)

        # Fallback: bounded job-log excerpt.
        try:
            jobs = await client.list_workflow_jobs_for_run(owner, repo, run_id)
        except GitHubAPIError as e:
            logger.warning("cicd_provider.jobs_fetch_failed", run_id=run_id, error=str(e))
            jobs = []

        failed_jobs = [j for j in jobs if j.get("conclusion") == "failure"]
        log_parts: list[str] = []
        for job in failed_jobs:
            try:
                log_text = await client.download_job_log(owner, repo, job["id"])
            except GitHubAPIError as e:
                logger.warning(
                    "cicd_provider.job_log_download_failed", job_id=job.get("id"), error=str(e)
                )
                continue
            if log_text:
                # Keep the *last* N lines -- the failure signal is almost
                # always near the end of a CI log, not the start.
                lines = log_text.splitlines()
                excerpt = "\n".join(lines[-max_log_lines:])
                log_parts.append(f"--- Job: {job.get('name', job['id'])} ---\n{excerpt}")

        if log_parts:
            return CIFailureContent(source="job_log", log_excerpt="\n\n".join(log_parts))

        # No annotations and no logs (or expired logs) is a normal outcome, not an error.
        return CIFailureContent(source="unavailable")
