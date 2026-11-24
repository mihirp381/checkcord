from datetime import datetime

from pydantic import BaseModel


class GitHubRepo(BaseModel):
    full_name: str
    private: bool


class GitHubWorkflowRunInfo(BaseModel):
    id: int
    name: str
    head_branch: str
    head_sha: str
    status: str
    conclusion: str | None = None
    html_url: str
    created_at: datetime
    updated_at: datetime
    # Relative workflow file path, e.g. ".github/workflows/ci.yml"; used to
    # refresh workflow_definitions on each delivery.
    path: str | None = None


class GitHubWorkflowRunPayload(BaseModel):
    action: str
    workflow_run: GitHubWorkflowRunInfo
    repository: GitHubRepo
