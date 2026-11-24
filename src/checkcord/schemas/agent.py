"""Agent transcript request and response shapes."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class AgentToolCall(BaseModel):
    """A single tool invocation extracted from an agent transcript."""

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentTrajectoryStep(BaseModel):
    """A single step from an agent session transcript (JSONL line)."""

    step_index: int | None = None
    source: str | None = None
    type: str | None = None
    status: str | None = None
    content: str | None = None
    tool_calls: list[AgentToolCall] = Field(default_factory=list)
    thinking: str | None = None


class AgentTrajectoryPayload(BaseModel):
    """Payload schema for agent trajectory ingestion.

    Supports two modes:
    1. Structured JSONL: Pass `steps` as a list of parsed transcript steps.
    2. Raw transcript: Pass `raw_transcript` as a plain text string
       (Markdown, logs, etc.).
    At least one of `steps` or `raw_transcript` must be provided.
    """

    # Optional overrides
    event_id: UUID | None = None
    session_id: str | None = None
    agent_name: str | None = None
    repository: str | None = None
    task_description: str | None = None
    happened_at: datetime | None = None

    # Transcript data — at least one must be provided
    steps: list[AgentTrajectoryStep] | None = None
    raw_transcript: str | None = None

    # Extensibility
    metadata: dict[str, Any] = Field(default_factory=dict)
