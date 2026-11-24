"""Typed wire payload for the ci.pr.context_checked stream."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class AffectedContext(BaseModel):
    """One AST-drift finding, as produced by ContextMatcherService.match_contexts."""

    symbol_name: str
    file_path: str
    node_type: str
    change_type: Literal["modified", "deleted"]
    canonical_ids: list[UUID] = Field(default_factory=list)


class PRContextCheckedPayload(BaseModel):
    """The full ci.pr.context_checked message.

    Published by ``workers/pr_evaluator.py`` (``.model_dump_json()`` as a
    single stream field) and parsed by
    ``workers/pr_status_reporter.py::_process_message`` instead of pulling
    individual fields out of a flat, untyped dict.
    """

    repository_full_name: str
    pull_request_number: int
    head_sha: str
    tenant_id: UUID
    affected_contexts: list[AffectedContext] = Field(default_factory=list)
    head_ref: str = ""
    diff: str = ""
    attempt: int = 1
    verdicts: dict[str, tuple[str, str]] = Field(default_factory=dict)
