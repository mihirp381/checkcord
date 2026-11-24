from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from checkcord.models.enums import ASTEdgeStatus, Severity


class Base(DeclarativeBase):
    pass


def _default_access_control() -> dict[str, Any]:
    """Fail-closed default matching ``AccessControl``'s own Pydantic defaults.

    Used before any per-context aggregation has run (see
    ``storage.postgres.aggregate_access_control``).
    """
    return {
        "is_public": False,
        "visibility": "private",
        "acl_type": "open",
        "allowed_identity_ids": [],
        "source_group_id": None,
        "inherited": False,
        "admin_overrides": [],
    }


class SlackInstallation(Base):
    """A Slack workspace connected to a tenant, with its bot token."""

    __tablename__ = "slack_installations"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    team_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        unique=True,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    bot_token: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    installation_info: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )


class MSTeamsInstallation(Base):
    """A Microsoft tenant connected to a tenant through Teams admin consent."""

    __tablename__ = "msteams_installations"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    ms_tenant_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        unique=True,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    auth_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )


class GithubInstallation(Base):
    """A GitHub repository or organization connected to a tenant, with token and webhook secret."""

    __tablename__ = "github_installations"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    repo_identifier: Mapped[str] = mapped_column(
        String,
        nullable=False,
        unique=True,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    personal_access_token: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    webhook_secret: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )


class JiraInstallation(Base):
    """A Jira site connected to a tenant, with its token and webhook secret."""

    __tablename__ = "jira_installations"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    jira_host: Mapped[str] = mapped_column(
        String,
        nullable=False,
        unique=True,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    personal_access_token: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    webhook_secret: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    poll_cursor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )


class APIKey(Base):
    """An API key, stored as its SHA-256. ``is_admin`` is set only when the key is provisioned."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    hashed_token: Mapped[str] = mapped_column(
        String,
        nullable=False,
        unique=True,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="Default API Key",
        server_default="Default API Key",
    )
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    is_admin: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["enterprise_users.id", "enterprise_users.tenant_id"],
            name="fk_api_keys_user_tenant",
            ondelete="CASCADE",
        ),
    )


class NotionInstallation(Base):
    """A Notion workspace connected to a tenant, with its encrypted token."""

    __tablename__ = "notion_installations"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    notion_workspace_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        unique=True,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    access_token: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    webhook_secret: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
    )
    poll_cursor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    poll_resume: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )


class LinearInstallation(Base):
    """A Linear organization connected to a tenant, with its encrypted token."""

    __tablename__ = "linear_installations"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    linear_organization_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        unique=True,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    access_token: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    webhook_secret: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
    )
    poll_cursor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )


class DatadogInstallation(Base):
    """A Datadog webhook connected to a tenant."""

    __tablename__ = "datadog_installations"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    webhook_secret: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )


class SentryInstallation(Base):
    """A Sentry webhook connected to a tenant."""

    __tablename__ = "sentry_installations"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    webhook_secret: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )


class EnterpriseUser(Base):
    """A person, matched across every connected system."""

    __tablename__ = "enterprise_users"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    primary_email: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
    )
    display_name: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    is_admin: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    scim_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "primary_email", name="uq_enterprise_users_tenant_email"),
        UniqueConstraint("id", "tenant_id", name="uq_enterprise_users_id_tenant"),
    )


class UserSession(Base):
    """A browser sign-in. Only the SHA-256 of the cookie value is stored."""

    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    hashed_token: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["enterprise_users.id", "enterprise_users.tenant_id"],
            name="fk_user_sessions_user_tenant",
            ondelete="CASCADE",
        ),
        Index("ix_user_sessions_tenant_user", "tenant_id", "user_id"),
    )


class ChatUser(Base):
    """A Slack or Teams account."""

    __tablename__ = "chat_users"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    platform: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    native_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    email: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )
    internal_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("enterprise_users.id"),
        nullable=True,
        index=True,
    )
    # 'email', 'profile' or 'manual'; a manual row is never relinked by a sync.
    link_source: Mapped[str | None] = mapped_column(String, nullable=True)
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    linked_by: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "platform", "native_id", name="uq_chat_users_tenant_platform_native"
        ),
    )


class ChatChannel(Base):
    """A Slack channel or Teams channel or chat."""

    __tablename__ = "chat_channels"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    platform: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    native_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "platform", "native_id", name="uq_chat_channels_tenant_platform_native"
        ),
    )


class ChatGroup(Base):
    """A Slack user group or Teams team."""

    __tablename__ = "chat_groups"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    platform: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    native_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "platform", "native_id", name="uq_chat_groups_tenant_platform_native"
        ),
    )


class JiraProjectKey(Base):
    """Jira Project Keys configured per tenant to avoid regex false positives."""

    __tablename__ = "jira_project_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    jira_installation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("jira_installations.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_key: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "project_key", name="uq_jira_project_keys_tenant_project_key"
        ),
    )


class ZoomInstallation(Base):
    """A Zoom account connected to a tenant, with its webhook secret."""

    __tablename__ = "zoom_installations"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    zoom_account_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        unique=True,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    webhook_secret: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )


class MsTeamsMeetingInstallation(Base):
    """Teams meeting transcripts: the clientState secret and the Graph subscription kept renewed."""

    __tablename__ = "msteams_meeting_installations"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    ms_tenant_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        unique=True,
        index=True,
    )
    webhook_secret: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    subscription_id: Mapped[str | None] = mapped_column(String, nullable=True)
    subscription_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    subscription_error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )


class Skill(Base):
    """A reusable skill made from agent sessions."""

    __tablename__ = "skills"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    description: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    instructions: Mapped[str] = mapped_column(
        String,  # TEXT
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )


class AgentLedger(Base):
    """A key-value entry agents keep, scoped by user, project or repository."""

    __tablename__ = "agent_ledgers"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    scope_type: Mapped[str] = mapped_column(
        String,  # e.g., 'user', 'project', 'repository'
        nullable=False,
    )
    scope_id: Mapped[str] = mapped_column(
        String,  # e.g., primary email, project key, repo name/url
        nullable=False,
    )
    key: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    value: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    agent_session_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "scope_type", "scope_id", "key", name="uq_agent_ledgers_scope_key"
        ),
    )


class CanonicalContext(Base):
    """A record: a decision, constraint, risk or reference made from events."""

    __tablename__ = "canonical_contexts"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    summary: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    # Served instead of ``summary``; written with it. NULL until computed.
    summary_redacted: Mapped[str | None] = mapped_column(Text, nullable=True)
    redaction_version: Mapped[str | None] = mapped_column(String, nullable=True)
    # The model pass failed; ``summary_redacted`` is regex-only until the sweep retries it.
    redaction_pending: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    confidence_score: Mapped[float] = mapped_column(
        nullable=False,
        default=0.0,
        server_default="0.0",
    )
    embedding: Mapped[list[float] | None] = mapped_column(
        # Width set by `checkcord migrate` from EMBEDDING_DIMENSION; see storage/vector_column.py.
        Vector(),
        nullable=True,
    )
    is_locked: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    severity: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default=Severity.SOFT_WARN.value,
        server_default=Severity.SOFT_WARN.value,
    )
    # The event whose audience the record takes.
    base_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )
    # The connection key it is held under while that connection is in review; None is live.
    held_connection: Mapped[str | None] = mapped_column(String, nullable=True)
    context_type: Mapped[str] = mapped_column(
        # 'general' (default) | 'adr' | 'constraint' | 'risk' | 'business_requirement'.
        # Set once when the record is created and not changed on a later merge.
        String,
        nullable=False,
        default="general",
        server_default="general",
    )
    content_hash: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default="",
    )
    last_verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    summary_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', summary)", persisted=True),
        nullable=True,
    )
    access_control: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=_default_access_control,
        server_default=(
            '{"is_public": false, "visibility": "private", "acl_type": "open", '
            '"allowed_identity_ids": [], "source_group_id": null, "inherited": false, '
            '"admin_overrides": []}'
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )
    # Hidden from every read unless ``app.include_retired`` is set; see tenant_context.
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_by: Mapped[str | None] = mapped_column(String, nullable=True)
    retired_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("canonical_contexts.id", ondelete="SET NULL"),
        nullable=True,
    )


class RecordReplacementSuggestion(Base):
    """A suggestion that ``replacement_id`` replaces ``record_id``; open until decided."""

    __tablename__ = "record_replacement_suggestions"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    record_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("canonical_contexts.id", ondelete="CASCADE"),
        nullable=False,
    )
    replacement_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("canonical_contexts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 'stated': the new record says it replaces an earlier decision. 'model': a model judged so.
    basis: Mapped[str] = mapped_column(String, nullable=False)
    similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismissed_by: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "record_id", "replacement_id", name="uq_record_replacement_suggestions"
        ),
        CheckConstraint("basis IN ('stated', 'model')", name="ck_replacement_basis"),
        CheckConstraint("record_id <> replacement_id", name="ck_replacement_not_self"),
    )


class ContextEventLink(Base):
    """Links a record to its events; no foreign key, since the events table is partitioned."""

    __tablename__ = "context_event_links"

    canonical_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("canonical_contexts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    # The redacted start of a later event that agrees with the record; None for the origin.
    excerpt_redacted: Mapped[str | None] = mapped_column(Text, nullable=True)
    excerpt_version: Mapped[str | None] = mapped_column(String, nullable=True)
    excerpt_pending: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class ContextRepoScope(Base):
    """Scope rows for a CanonicalContext.

    A record with no rows is org-global; with rows it is scoped to exactly
    those ``(scope_type, scope_value)`` pairs. ``scope_type`` is ``"team"``,
    ``"repo"`` or ``"path"``; ``"org"`` is never stored. ``"team"`` is a
    free-text label with no backing table. ``priority`` is computed by the
    server from ``scope_type`` at write time (path=3 > repo=2 > team=1) and
    decides which row wins when several match a request.
    """

    __tablename__ = "context_repo_scopes"

    canonical_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("canonical_contexts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    scope_type: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default="repo",
        server_default="repo",
    )
    scope_value: Mapped[str] = mapped_column(
        String,
        primary_key=True,
    )
    priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=2,
        server_default="2",
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class ContextScopeAuditLog(Base):
    """Audit trail for human edits to a CanonicalContext's scope.

    Records every change to a record's scope set or ``is_locked``, with the
    prior values. ``previous_scope_repo_identifiers`` and
    ``new_scope_repo_identifiers`` hold the repo-tier list as JSON (``null``
    means org-global). ``previous_scope_rows`` and ``new_scope_rows`` hold the
    full typed set (``[{"scope_type": ..., "scope_value": ...}, ...]``).
    """

    __tablename__ = "context_scope_audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    canonical_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("canonical_contexts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    changed_by: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    previous_scope_repo_identifiers: Mapped[list[str] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    new_scope_repo_identifiers: Mapped[list[str] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    previous_scope_rows: Mapped[list[dict[str, str]] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    new_scope_rows: Mapped[list[dict[str, str]] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    previous_is_locked: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
    )
    new_is_locked: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class UserRepoAffinity(Base):
    """Per-(tenant, user, repo) usage signal.

    Records how often and how recently a user's MCP queries resolved to a
    repo; the input to personalised retrieval. Written only for a resolved
    ``user_id``, never for ``ANONYMOUS_USER_ID``, and not used by the web viewer.
    """

    __tablename__ = "user_repo_affinity"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
    )
    repo_identifier: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    query_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    last_queried_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "repo_identifier", name="uq_user_repo_affinity_identifier"
        ),
    )


class AuditLogEntry(Base):
    """One read of record content, one admin action or one failed sign-in burst.

    Append-only for ``checkcord_app`` and tenant-isolated by RLS. Written through
    ``storage.audit.record`` in the same transaction as the read it describes.
    """

    __tablename__ = "audit_log_entries"
    __table_args__ = (
        CheckConstraint(
            "channel IN ('mcp', 'api', 'cli', 'scim')", name="ck_audit_log_entries_channel"
        ),
        Index(
            "ix_audit_log_entries_tenant_time",
            "tenant_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        Index("ix_audit_log_entries_tenant_user", "tenant_id", "user_id", text("created_at DESC")),
        Index("ix_audit_log_entries_tenant_key", "tenant_id", "key_id", text("created_at DESC")),
        Index("ix_audit_log_entries_tenant_action", "tenant_id", "action", text("created_at DESC")),
        Index("ix_audit_log_entries_record_ids", "record_ids", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    channel: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    user_email: Mapped[str | None] = mapped_column(String, nullable=True)
    key_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    key_name: Mapped[str | None] = mapped_column(String, nullable=True)
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    record_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default="{}"
    )
    redacted_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    result_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AudienceContainer(Base):
    """Who can read one Slack channel, Teams conversation or GitHub repository.

    Events whose ``audience_container`` names it carry this audience.
    """

    __tablename__ = "audience_containers"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "platform", "container", name="uq_audience_containers_container"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    platform: Mapped[str] = mapped_column(String, nullable=False)
    container: Mapped[str] = mapped_column(String, nullable=False)
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    member_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False, default=list, server_default="{}"
    )
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    unmatched_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    unmatched_accounts: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class ConnectionReview(Base):
    """Whether material from one connection is held for review before agents can read it."""

    __tablename__ = "connection_reviews"

    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    connection: Mapped[str] = mapped_column(String, primary_key=True)
    in_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class ASTNode(Base):
    """A parsed and hashed code symbol (class, function, method)."""

    __tablename__ = "ast_nodes"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    repo_identifier: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    file_path: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    symbol_name: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    node_type: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    merkle_hash: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "repo_identifier",
            "file_path",
            "symbol_name",
            "node_type",
            name="uq_ast_nodes_identifier",
        ),
    )


class ASTContextEdge(Base):
    """Links a code symbol to a record."""

    __tablename__ = "ast_context_edges"

    ast_node_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ast_nodes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    canonical_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("canonical_contexts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    status: Mapped[ASTEdgeStatus] = mapped_column(
        Enum(
            ASTEdgeStatus,
            name="ast_edge_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=ASTEdgeStatus.ACTIVE,
        server_default="active",
    )
    last_accessed: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )


class WorkflowDefinitionRecord(Base):
    """Parsed job/step graph of one `.github/workflows/*.yml` file.

    Refreshed on each `workflow_run` webhook delivery for that ref. Keyed on
    (tenant_id, repo_identifier, workflow_path), so a refresh is an upsert.
    """

    __tablename__ = "workflow_definitions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "repo_identifier",
            "workflow_path",
            name="uq_workflow_definitions_identifier",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    repo_identifier: Mapped[str] = mapped_column(String, nullable=False)
    workflow_path: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    jobs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    commit_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )


class CIFailureContextLink(Base):
    """Links a CI failure event to the records and AST node it most likely affected.

    File granularity. Events live in a partitioned table without foreign-key
    support, so `ci_event_id` is not enforced by the database.
    """

    __tablename__ = "ci_failure_context_links"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    ci_event_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, index=True
    )
    canonical_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("canonical_contexts.id", ondelete="CASCADE"),
        nullable=False,
    )
    ast_node_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ast_nodes.id", ondelete="SET NULL"),
        nullable=True,
    )
    file_path: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "ci_event_id",
            "canonical_id",
            "file_path",
            name="uq_ci_failure_context_links_identifier",
        ),
    )


class ModelUsage(Base):
    """Chat, embedding and rerank calls per tenant, day, kind, purpose and model. No content."""

    __tablename__ = "model_usage"

    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    kind: Mapped[str] = mapped_column(String, primary_key=True)
    purpose: Mapped[str] = mapped_column(String, primary_key=True)
    model: Mapped[str] = mapped_column(String, primary_key=True)
    calls: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    failures: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    latency_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class ScheduledJob(Base):
    """When each scheduled job last started and finished, shared by every API process."""

    __tablename__ = "scheduled_jobs"

    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConnectionScope(Base):
    """Which containers of one connection are ingested. Exclude wins; a non-empty include
    list means only what it lists."""

    __tablename__ = "connection_scopes"

    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    connection: Mapped[str] = mapped_column(String, primary_key=True)
    include: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list, server_default="{}"
    )
    exclude: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list, server_default="{}"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class ConnectionAudience(Base):
    """Who can read what an admin-audience connection ingests, optionally per container.
    The row for the container wins over the connection's row."""

    __tablename__ = "connection_audiences"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    connection: Mapped[str] = mapped_column(String, nullable=False)
    container: Mapped[str | None] = mapped_column(String, nullable=True)
    mode: Mapped[str] = mapped_column(String, nullable=False, server_default="nobody")
    groups: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list, server_default="{}"
    )
    people: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list, server_default="{}"
    )
    member_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False, default=list, server_default="{}"
    )
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    recompute_state: Mapped[str | None] = mapped_column(String, nullable=True)
    recompute_done: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    recompute_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    recomputed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConnectionScopeSkip(Base):
    """Events a connection's scope kept out, per day."""

    __tablename__ = "connection_scope_skips"

    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    connection: Mapped[str] = mapped_column(String, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class ScimGroup(Base):
    """A group pushed by the identity provider over SCIM."""

    __tablename__ = "scim_groups"
    __table_args__ = (
        Index("uq_scim_groups_name", "tenant_id", func.lower(text("display_name")), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class ScimGroupMember(Base):
    __tablename__ = "scim_group_members"

    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    group_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("scim_groups.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("enterprise_users.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )


class ConnectorInstallation(Base):
    """A connection of a connector that keeps no table of its own."""

    __tablename__ = "connector_installations"
    __table_args__ = (UniqueConstraint("tenant_id", "provider", "identifier"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    identifier: Mapped[str] = mapped_column(String, nullable=False)
    secret: Mapped[str | None] = mapped_column(String, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class RemovedSource(Base):
    """A removed item, by a hash of its source id, so a later delivery is not stored again."""

    __tablename__ = "removed_sources"

    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    source_platform: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    removed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
