from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from checkcord.models.common import AccessControl
from checkcord.models.enums import SourcePlatform


class RawChatPayload(BaseModel):
    """Raw chat payload emitted by Slack/MS Teams adapters before screening/scrubbing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload_id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    platform: SourcePlatform
    channel_id: str
    channel_name: str | None = None
    group_id: str | None = None
    author_id: str | None = None
    author_email: str | None = None
    author_name: str | None = None
    message_id: str
    thread_ts: str | None = None
    happened_at: datetime
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    content_raw: str
    content_clean: str
    raw_event: dict[str, Any] = Field(default_factory=dict)
    access_control: AccessControl
    connection: str | None = None
    payload_version: int = 1

    def to_redis_dict(self) -> dict[str, str]:
        """Serializes for Redis Streams XADD.

        Flattens dict/list into JSON strings, UUIDs and datetimes to ISO strings.
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
    def from_redis_dict(cls, data: dict[str, str]) -> RawChatPayload:
        """Reconstructs a RawChatPayload from a flat Redis Stream dictionary."""
        parsed_data: dict[str, Any] = {}
        for key, value in data.items():
            if value == "__NONE__":
                parsed_data[key] = None
                continue

            if key in {"raw_event", "access_control"}:
                parsed_data[key] = json.loads(value)
            elif key in {"payload_version"}:
                parsed_data[key] = int(value)
            else:
                parsed_data[key] = value

        return cls(**parsed_data)
