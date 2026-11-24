from typing import Any

from pydantic import BaseModel, Field


class JiraUser(BaseModel):
    accountId: str = Field(alias="accountId")
    displayName: str = Field(alias="displayName")
    emailAddress: str | None = Field(default=None, alias="emailAddress")
    active: bool | None = None


class JiraStatus(BaseModel):
    name: str


class JiraIssueType(BaseModel):
    id: str
    name: str
    subtask: bool


class JiraIssueFields(BaseModel):
    summary: str
    description: dict[str, Any] | str | None = None
    status: JiraStatus
    issuetype: JiraIssueType | None = None
    assignee: JiraUser | None = None
    reporter: JiraUser | None = None
    created: str | None = None
    updated: str | None = None


class JiraIssue(BaseModel):
    id: str
    key: str
    self: str
    fields: JiraIssueFields


class JiraComment(BaseModel):
    id: str
    self: str
    body: dict[str, Any] | str
    author: JiraUser
    created: str
    updated: str | None = None


class JiraChangelogItem(BaseModel):
    field: str
    fieldId: str | None = None
    from_: str | None = Field(default=None, alias="from")
    fromString: str | None = None
    to: str | None = None
    toString: str | None = None


class JiraChangelog(BaseModel):
    id: str | None = None
    items: list[JiraChangelogItem] = Field(default_factory=list)


class JiraWebhookPayload(BaseModel):
    timestamp: int
    webhookEvent: str
    issue_event_type_name: str | None = None
    user: JiraUser | None = None
    issue: JiraIssue | None = None
    comment: JiraComment | None = None
    changelog: JiraChangelog | None = None
