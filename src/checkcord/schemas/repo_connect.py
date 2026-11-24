"""Request schema for POST /api/v1/admin/repos/connect."""

from pydantic import BaseModel, Field


class RepoConnectRequest(BaseModel):
    """Registers a GithubInstallation and triggers a full-repo AST backfill."""

    repo_identifier: str = Field(
        description='"org/repo", matching GithubInstallation.repo_identifier'
    )
    personal_access_token: str = Field(description="GitHub PAT used for all API calls to this repo")
    webhook_secret: str = Field(
        description="Shared secret validating this repo's webhook deliveries"
    )
