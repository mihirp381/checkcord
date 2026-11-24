"""Documents from Jira, Notion and Linear as blocks."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class DocumentBlock(BaseModel):
    """An atomic structural node (paragraph, code, heading, list, table) in a document."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str = Field(
        ..., description="Block type, e.g. heading, paragraph, code_block, list_item, unhandled"
    )
    text: str = Field(default="", description="Clean text extracted from this block")
    raw_json: dict[str, Any] = Field(
        default_factory=dict, description="Original raw AST node dict for debugging"
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Block-specific metadata (e.g. language, depth)"
    )
    children: list[DocumentBlock] = Field(default_factory=list, description="Nested child blocks")


class Document(BaseModel):
    """Unified internal representation of an ingested document/ticket (Jira, Notion, Linear)."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str = Field(..., description="Document or ticket title")
    source_platform: str = Field(..., description="Source platform name: jira, notion, linear")
    source_id: str = Field(
        ..., description="Native ID on the source platform (e.g., PROJ-123 or Notion UUID)"
    )
    blocks: list[DocumentBlock] = Field(
        default_factory=list, description="Top-level ordered blocks"
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Document-level metadata (author, tags, status)"
    )
    raw_markdown_fallback: str = Field(
        default="", description="Full raw markdown representation for fallback"
    )
