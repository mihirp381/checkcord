from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class Skill(BaseModel):
    """A reusable skill made from agent sessions."""

    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    name: str
    description: str
    instructions: str  # Markdown containing YAML frontmatter
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
