"""Graph API response shapes."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from checkcord.core.agent_text import NOTICE

# Schemas for the Graph endpoints.
# Everything related to sync of the codebase, GraphViz and Context-Summary related.

RecordTypeName = Literal["adr", "constraint", "business_requirement", "risk", "general"]


def _notice() -> Any:
    return Field(NOTICE, description="How to treat the record text in this response")


class ContextSummary(BaseModel):
    """A single canonical context summary with its confidence score."""

    summary: str = Field(
        ...,
        description="The synthesized canonical context summary text",
    )
    confidence_score: float = Field(
        ...,
        description="The calculated confidence score of the context",
    )


class ContextLookupResponse(BaseModel):
    """Records attached to a symbol."""

    notice: str = _notice()
    contexts: list[ContextSummary]


class ImpactWarningHeader(BaseModel):
    """Constraints and dependencies to warn about before a change."""

    notice: str = _notice()
    symbol: str = Field(..., description="The symbol name queried")
    warning_header: str = Field(
        ...,
        description=(
            "A pre-formatted markdown warning header to prepend to an LLM's system prompt"
        ),
    )


class ActiveDecisionsResponse(BaseModel):
    """Response model for retrieving the highest-confidence global architectural rules.

    Queries the repository identifier.
    """

    notice: str = _notice()
    repo_identifier: str = Field(..., description="The repository identifier queried")
    markdown_content: str = Field(
        ...,
        description=(
            "An aggregated markdown string representing active architectural decisions "
            "suitable for a .context/ARCHITECTURE.md static prompt prefix."
        ),
    )


class ASTNodeSyncItem(BaseModel):
    """Data payload for a single AST node during sync."""

    file_path: str = Field(..., description="Path to the file containing this node")
    symbol_name: str = Field(..., description="The name of the code symbol")
    node_type: str = Field(..., description="Type of AST node (e.g. class, function, method)")
    merkle_hash: str = Field(
        ...,
        description="Whitespace-and-comment-stripped SHA-256 hash of the node",
    )


class ASTSyncPayload(BaseModel):
    """Request payload for the AST sync endpoint."""

    repo_identifier: str = Field(
        ...,
        description="The repository identifier containing these nodes",
    )
    nodes: list[ASTNodeSyncItem] = Field(
        ...,
        description="List of AST nodes representing the current codebase state",
    )


class ASTSyncResponse(BaseModel):
    """Response payload returned immediately upon accepting AST sync request."""

    status: str = Field(..., description="The status of the sync request (e.g., 'accepted')")
    message: str = Field(..., description="Additional detail about the accepted request")


class GraphNode(BaseModel):
    """A node of the graph view."""

    id: str = Field(..., description="Unique node ID (e.g., file:src/calc.py)")
    type: str = Field(..., description="Node type: file, symbol, context, developer")
    label: str = Field(..., description="User-facing label for the node")
    node_type: str | None = Field(None, description="AST node type (only for symbol type)")
    confidence_score: float | None = Field(
        None, description="Confidence score (only for context type)"
    )


class GraphEdge(BaseModel):
    """An edge of the graph view."""

    source: str = Field(..., description="Source node ID")
    target: str = Field(..., description="Target node ID")


class NetworkGraphResponse(BaseModel):
    """The graph of one repository's records and code."""

    notice: str = _notice()
    nodes: list[GraphNode] = Field(..., description="List of nodes in the graph")
    edges: list[GraphEdge] = Field(..., description="List of edges in the graph")


class GraphRepository(BaseModel):
    repo_identifier: str
    files: int = Field(..., description="Files in the code graph")


class GraphRepositories(BaseModel):
    items: list[GraphRepository]


class NodeDetailsResponse(BaseModel):
    """Details of one graph node."""

    notice: str = _notice()
    type: str = Field(..., description="Node type: file, symbol, context, developer")
    id: str = Field(..., description="Unique node ID")
    file_path: str | None = Field(None, description="File path (for file and symbol type)")
    repo_identifier: str | None = Field(None, description="Repository identifier")
    symbols: list[dict] | None = Field(None, description="List of symbols in a file")
    symbol_name: str | None = Field(None, description="Symbol name (for symbol type)")
    node_type: str | None = Field(None, description="AST node type (for symbol type)")
    merkle_hash: str | None = Field(None, description="Merkle hash (for symbol type)")
    confidence_score: float | None = Field(None, description="Confidence score (for context type)")
    summary: str | None = Field(
        None, description="Synthesis summary/decision text (for context type)"
    )
    sources: list[dict] | None = Field(
        None,
        description=(
            "Event sources (for context type): platform, type, time, author and, for chat, "
            "channel, link and message counts. Never their text."
        ),
    )
    updates: list[dict] | None = Field(
        None, description="Redacted update excerpts the caller may read, newest first"
    )
    contexts: list[dict] | None = Field(
        None, description="List of canonical contexts linked to symbol"
    )
    display_name: str | None = Field(
        None, description="Developer display name (for developer type)"
    )
    authored_events_count: int | None = Field(
        None, description="Number of events authored by developer"
    )
    scope_repo_identifiers: list[str] | None = Field(
        None,
        description=(
            "Governance scope for context type: empty list means org-global, "
            "otherwise the repo(s) this context is scoped to"
        ),
    )
    is_locked: bool | None = Field(
        None, description="Whether this context's scope is locked against auto-override"
    )
    context_type: str | None = Field(
        None,
        description=(
            "'general' (default), 'adr', 'constraint', 'risk', or "
            "'business_requirement' (for context type)."
        ),
    )
    content_hash: str | None = Field(
        None, description="SHA-256 hash of the context's summary text (for context type)"
    )
    last_verified_at: str | None = Field(
        None,
        description="ISO8601 timestamp of when the summary/hash were last (re)computed",
    )
    summary_redacted: bool | None = Field(
        None,
        description=(
            "Whether redaction changed the summary text for this response "
            "(context/symbol-linked-context type)"
        ),
    )


class ScopeRow(BaseModel):
    """One typed scope assignment.

    ``scope_type`` is ``"team"``, ``"repo"`` or ``"path"``; an empty scope list
    means org-global. ``priority`` is computed by the server from ``scope_type``.
    """

    scope_type: str = Field(..., description='One of "team", "repo", or "path".')
    scope_value: str = Field(
        ..., description="The team label, repo identifier, or path string, per scope_type."
    )


class ScopeUpdateRequest(BaseModel):
    """Request payload for PATCH /api/v1/admin/records/{id}/scope.

    ``scope_rows`` and ``repo_identifiers`` (repo-only) are two ways to give
    the new scope; ``scope_rows`` wins when both are given. Neither, ``null``
    or ``[]`` means org-global. The value replaces the record's whole scope set.
    """

    repo_identifiers: list[str] | None = Field(
        default=None,
        description=(
            "Repo-only scope shape: null or [] for org-global, or one or more "
            "repo identifiers. Ignored when scope_rows is provided."
        ),
    )
    scope_rows: list[ScopeRow] | None = Field(
        default=None,
        description=(
            "Typed scope rows: null or [] for org-global, or one or more "
            "(scope_type, scope_value) rows spanning team/repo/path. Takes "
            "precedence over repo_identifiers when both are given."
        ),
    )
    is_locked: bool = Field(
        ..., description="Whether this scope assignment should be locked against auto-override"
    )
    changed_by: str | None = Field(
        default=None,
        description=(
            "Accepted for compatibility and ignored; the audit log records the "
            "API key's bound user, or the key name when unbound"
        ),
    )


class ScopeUpdateResponse(BaseModel):
    """Response payload after a successful scope update."""

    id: str = Field(..., description="The canonical context id")
    scope_repo_identifiers: list[str] = Field(
        default_factory=list,
        description=(
            "Repo-tier scope set after the update (empty list = org-global or "
            "scoped only by team or path). See scope_rows for the full typed set."
        ),
    )
    scope_rows: list[ScopeRow] = Field(
        default_factory=list,
        description="The new full typed scope set after the update, across all tiers.",
    )
    is_locked: bool = Field(..., description="The new lock state after the update")


class SeverityUpdateRequest(BaseModel):
    """A blocking record fails the pull-request check; an advisory one is only cited."""

    severity: Literal["blocking", "advisory"]


class SeverityUpdateResponse(BaseModel):
    id: str
    severity: Literal["blocking", "advisory"]


class TypeUpdateRequest(BaseModel):
    """Decision types are adr, constraint, business_requirement and risk; general is reference."""

    type: RecordTypeName


class TypeUpdateResponse(BaseModel):
    id: str
    type: RecordTypeName


class ScopeHistoryResponse(BaseModel):
    """Most recent scope or is_locked change for a record, shown as "last changed by X at Y"."""

    changed_by: str | None = Field(
        default=None,
        description="Who made the most recent scope/lock change, or null if never edited.",
    )
    changed_at: str | None = Field(
        default=None,
        description="ISO timestamp of the most recent change, or null if never edited.",
    )
