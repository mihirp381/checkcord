from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class GitHubUser(BaseModel):
    login: str
    email: str | None = None


class GitHubRepo(BaseModel):
    full_name: str
    private: bool


class GitHubPRHeadBase(BaseModel):
    ref: str
    sha: str | None = None


class GitHubPullRequest(BaseModel):
    number: int
    title: str
    body: str | None = None
    created_at: datetime
    html_url: str
    user: GitHubUser
    head: GitHubPRHeadBase
    base: GitHubPRHeadBase
    merged: bool = False
    merge_commit_sha: str | None = None
    merged_at: datetime | None = None


# ── Webhook Payloads ───────────────────────────────────────────────────────


class GitHubPRPayload(BaseModel):
    action: str
    number: int
    pull_request: GitHubPullRequest
    repository: GitHubRepo


class GitHubPRReview(BaseModel):
    id: int
    body: str | None = None
    state: str
    submitted_at: datetime
    user: GitHubUser


class GitHubPRReviewPayload(BaseModel):
    action: str
    review: GitHubPRReview
    pull_request: GitHubPullRequest
    repository: GitHubRepo


class GitHubComment(BaseModel):
    id: int
    body: str
    created_at: datetime
    user: GitHubUser


class GitHubIssueCommentPayload(BaseModel):
    action: str
    comment: GitHubComment
    issue: dict[str, Any] = Field(default_factory=dict)
    repository: GitHubRepo


class GitHubPRReviewCommentPayload(BaseModel):
    action: str
    comment: GitHubComment
    pull_request: GitHubPullRequest
    repository: GitHubRepo


class GitHubCommitAuthor(BaseModel):
    name: str
    email: str
    username: str | None = None


class GitHubCommit(BaseModel):
    id: str
    message: str
    timestamp: datetime
    author: GitHubCommitAuthor
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    modified: list[str] = Field(default_factory=list)


class GitHubPushPayload(BaseModel):
    ref: str
    before: str
    after: str
    commits: list[GitHubCommit] = Field(default_factory=list)
    repository: GitHubRepo
