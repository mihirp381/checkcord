"""CI run models."""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from checkcord.models.common import FrozenModel


class WorkflowStepDefinition(BaseModel):
    """One step within a workflow job (either a `uses:` action or a `run:` command)."""

    name: str | None = None
    uses: str | None = None
    run: str | None = None


class WorkflowJobDefinition(BaseModel):
    """One job within a workflow, including its `needs:` dependency edges."""

    job_id: str
    name: str | None = None
    needs: list[str] = Field(default_factory=list)
    steps: list[WorkflowStepDefinition] = Field(default_factory=list)


class WorkflowDefinition(BaseModel):
    """The parsed job/step graph of one `.github/workflows/*.yml` file."""

    workflow_path: str
    name: str | None = None
    jobs: list[WorkflowJobDefinition] = Field(default_factory=list)


class CIFailureAnnotation(BaseModel):
    """One Checks-API annotation: structured failure content, more precise than a job log."""

    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    message: str
    annotation_level: str | None = None


class CIFailureContent(BaseModel):
    """Result of `GitHubCICDProvider.fetch_failure_content`.

    Structured annotations, a bounded job-log excerpt, or neither when both
    are unavailable (expired logs, no annotations), which is not an error.
    """

    source: Literal["annotations", "job_log", "unavailable"]
    annotations: list[CIFailureAnnotation] = Field(default_factory=list)
    log_excerpt: str | None = None


class ExtractedCIFailureContent(FrozenModel):
    """LLM-facing CI-failure extraction schema.

    Has no `id` field so the model is not asked to invent one;
    `ExtractedCIFailure` adds the id after parsing. `line_number` is
    informational only; failures link to code at file level.
    """

    failing_test_name: str | None = None
    file_path: str | None = None
    line_number: int | None = None
    error_message: str
    summary: str


class ExtractedCIFailure(ExtractedCIFailureContent):
    """Structured failure signal extracted from CI failure content."""

    id: UUID = Field(default_factory=uuid4)
