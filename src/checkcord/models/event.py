import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from checkcord.models.common import (
    AccessControl,
    AgentMetadata,
    AuthorInfo,
    ChatMetadata,
    CodeMetadata,
    DocumentMetadata,
    EntityReference,
    IncidentMetadata,
    MeetingMetadata,
    TaskMetadata,
)
from checkcord.models.enums import (
    EventAction,
    SignalStatus,
    SourcePlatform,
    SourceType,
)


class CanonicalEvent(BaseModel):
    """An event as every pipeline stage reads it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Identifiers
    event_id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    parent_event_id: UUID | None = None
    fingerprint: str = ""  # Auto-generated if empty
    schema_version: int = 1

    # Source Origin
    source_platform: SourcePlatform  # Enum. Forcing to be from the list of allowed items.
    source_type: SourceType
    event_action: EventAction = EventAction.CREATED
    source_id: str
    version: int = 1

    # Temporal
    happened_at: datetime
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Payload
    author: AuthorInfo
    content_raw: str
    content_clean: str
    source_raw_payload: dict[str, Any] = Field(default_factory=dict)

    # Governance & State
    access_control: AccessControl
    # The channel or repository whose stored audience this event carries.
    audience_container: str | None = None
    # The connection it came through, as "provider:identifier" from `checkcord connections`.
    connection: str | None = None
    signal_status: SignalStatus = SignalStatus.UNPROCESSED

    # Optional Domain Metadata
    code_metadata: CodeMetadata | None = None
    task_metadata: TaskMetadata | None = None
    document_metadata: DocumentMetadata | None = None
    agent_metadata: AgentMetadata | None = None
    incident_metadata: IncidentMetadata | None = None
    meeting_metadata: MeetingMetadata | None = None
    chat_metadata: ChatMetadata | None = None

    # Relationships & Extensibility
    entities: list[EntityReference] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _generate_fingerprint_and_validate(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Pre-init validation for timezone enforcement and fingerprinting."""
        if not isinstance(data, dict):
            return data

        # Enforce UTC on happened_at
        happened_at = data.get("happened_at")
        if isinstance(happened_at, datetime):
            if happened_at.tzinfo is None:
                raise ValueError("happened_at must be timezone-aware (UTC)")

        # Generate fingerprint if not provided
        if not data.get("fingerprint"):
            platform = data.get("source_platform", "")
            if hasattr(platform, "value"):
                platform = platform.value

            type_ = data.get("source_type", "")
            if hasattr(type_, "value"):
                type_ = type_.value

            source_id = str(data.get("source_id", ""))
            version = str(data.get("version", 1))

            raw_str = f"{platform}:{type_}:{source_id}:{version}"
            data["fingerprint"] = hashlib.sha256(raw_str.encode("utf-8")).hexdigest()

        return data

    def to_redis_dict(self) -> dict[str, str]:
        """
        Serializes for Redis Streams XADD.
        Flattens the dictionary; nested objects become JSON strings.
        UUIDs and datetimes are cast to strings.
        """
        raw_dict = self.model_dump(mode="json")
        redis_payload: dict[str, str] = {}

        for key, value in raw_dict.items():
            if value is None:
                redis_payload[key] = "__NONE__"
            elif isinstance(value, dict | list):
                redis_payload[key] = json.dumps(value)
            else:
                redis_payload[key] = str(value)

        return redis_payload

    @classmethod
    def from_redis_dict(cls, data: dict[str, str]) -> "CanonicalEvent":
        """
        Reconstructs a CanonicalEvent from a flat Redis Stream dictionary.
        """
        parsed_data: dict[str, Any] = {}
        for key, value in data.items():
            if value == "__NONE__":
                parsed_data[key] = None
                continue

            # Fields known to be nested objects or lists in JSON
            if key in {
                "author",
                "access_control",
                "code_metadata",
                "task_metadata",
                "document_metadata",
                "agent_metadata",
                "incident_metadata",
                "meeting_metadata",
                "chat_metadata",
                "entities",
                "metadata",
                "source_raw_payload",
            }:
                parsed_data[key] = json.loads(value)
            # version and schema_version are ints
            elif key in {"version", "schema_version"}:
                parsed_data[key] = int(value)
            else:
                parsed_data[key] = value

        return cls(**parsed_data)

    def to_postgres_dict(self) -> dict[str, Any]:
        """
        Serializes for SQLAlchemy asyncpg inserts.
        Keeps UUIDs and datetimes as native Python objects.
        Nested Pydantic models become plain dicts/lists for JSONB columns.
        """
        return self.model_dump(mode="python")
