from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from checkcord.models.common import (
    AccessControl,
    AuthorInfo,
    CodeMetadata,
    DocumentMetadata,
    EntityReference,
    TaskMetadata,
)
from checkcord.models.enums import (
    EventAction,
    SignalStatus,
    SourcePlatform,
    SourceType,
)


class CustomPushPayload(BaseModel):
    """Payload schema for custom data ingestion."""

    event_id: UUID | None = None
    parent_event_id: UUID | None = None
    fingerprint: str | None = None
    schema_version: int = 1
    source_platform: SourcePlatform = SourcePlatform.CUSTOM
    source_type: SourceType = SourceType.CUSTOM_PAYLOAD
    event_action: EventAction = EventAction.CREATED
    source_id: str | None = None
    version: int = 1
    happened_at: datetime | None = None
    author: AuthorInfo | None = None
    content_raw: str
    content_clean: str | None = None
    source_raw_payload: dict[str, Any] = Field(default_factory=dict)
    access_control: AccessControl | None = None
    signal_status: SignalStatus = SignalStatus.UNPROCESSED
    code_metadata: CodeMetadata | None = None
    task_metadata: TaskMetadata | None = None
    document_metadata: DocumentMetadata | None = None
    entities: list[EntityReference] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ACLOverridePayload(BaseModel):
    """Payload schema for overriding access control lists."""

    admin_overrides: list[UUID]
