from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from checkcord.models.enums import AccessVisibility, AclType


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AuthorInfo(FrozenModel):
    """Who made the event."""

    source_id: str
    internal_id: UUID | None = None
    email: str | None = None
    display_name: str | None = None
    bot_id: str | None = None


class AccessControl(FrozenModel):
    """Who may read the event."""

    is_public: bool = False
    visibility: AccessVisibility = AccessVisibility.PRIVATE
    acl_type: AclType = AclType.OPEN
    allowed_identity_ids: list[UUID] = Field(default_factory=list)
    source_group_id: str | None = None
    inherited: bool = False
    admin_overrides: list[UUID] = Field(default_factory=list)


class CodeMetadata(FrozenModel):
    """Code change fields (GitHub)."""

    repository: str
    branch: str
    files_changed: list[str] = Field(default_factory=list)
    lines_added: int = 0
    lines_removed: int = 0
    pull_request_id: str | None = None
    commit_sha: str | None = None
    merge_commit: bool = False
    primary_language: str | None = None
    is_rollback: bool = False


class TaskMetadata(FrozenModel):
    """Issue fields (Jira)."""

    status: str
    priority: str | None = None  # String or None. Default is None
    assignee_id: str | None = None
    project_key: str | None = None
    labels: list[str] = Field(default_factory=list)


class DocumentMetadata(FrozenModel):
    """Document fields (Notion)."""

    title: str
    url: str
    space_name: str | None = None
    tags: list[str] = Field(default_factory=list)


class IncidentMetadata(FrozenModel):
    """Incident and alert fields (Datadog, Sentry)."""

    severity: str
    environment: str
    affected_services: list[str] = Field(default_factory=list)
    resolved_at: datetime | None = None
    root_cause_identified: bool = False


class MeetingMetadata(FrozenModel):
    """Metadata specific to completed meeting recordings/transcripts (e.g., Zoom, MS Teams).

    Note: Ongoing/active meetings are strictly rejected via positive duration constraint.
    """

    duration_seconds: int = Field(
        gt=0, description="Duration in seconds. Must be > 0 (completed meetings only)."
    )
    attendee_count: int = Field(default=1, ge=1)
    meeting_type: str | None = None
    has_recording: bool = False


class ChatMetadata(FrozenModel):
    """Chat fields (Slack, Teams)."""

    channel_id: str
    thread_id: str | None = None
    reply_count: int = 0


class EntityReference(FrozenModel):
    """A cross-reference to another entity mentioned in this event."""

    entity_type: str  # e.g., 'user', 'channel', 'ticket', 'pr'
    entity_id: str
    entity_platform: str | None = None
    relationship: str | None = None


class AgentMetadata(FrozenModel):
    """Agent session fields."""

    session_id: str
    prompt: str | None = None
    reasoning_chain: list[str] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    files_read: list[str] = Field(default_factory=list)
    tokens_used: int = 0
    synthesized_skills: list[str] = Field(default_factory=list)
    model_name: str | None = None
    execution_time_ms: int | None = None
