"""FastAPI router for traversable graph API endpoints.

Record summaries and context-node labels are served from their stored
redaction, and every served text gets the regex pass of ``core/redaction.py``.
Nothing here calls a model.

Read endpoints run under the caller's identity: a key bound to a user is
filtered to what that user may see, and an unbound key (admin or not) gets
the anonymous identity, i.e. public content only. Scope edits need an admin
key and record the key's bound user (or key name) as the actor.
"""

from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.api.deps import (
    Principal,
    get_db_engine,
    get_principal,
    require_admin_principal,
)
from checkcord.api.push_router import verify_push_token
from checkcord.api.schemas import ERRORS
from checkcord.core import redaction
from checkcord.schemas.graph import (
    ActiveDecisionsResponse,
    ASTNodeSyncItem,
    ASTSyncPayload,
    ASTSyncResponse,
    ContextLookupResponse,
    ContextSummary,
    GraphRepositories,
    GraphRepository,
    ImpactWarningHeader,
    NetworkGraphResponse,
    NodeDetailsResponse,
    ScopeHistoryResponse,
    ScopeRow,
    ScopeUpdateRequest,
    ScopeUpdateResponse,
    SeverityUpdateRequest,
    SeverityUpdateResponse,
    TypeUpdateRequest,
    TypeUpdateResponse,
)
from checkcord.services import code_links, record_notes, records
from checkcord.storage import audit
from checkcord.storage.models import CanonicalContext
from checkcord.storage.postgres import PostgresEventStore, tenant_context

logger = structlog.get_logger(__name__).bind(component="graph_router")

router = APIRouter(tags=["graph"], responses=ERRORS)
records_router = APIRouter(tags=["admin: records"], responses=ERRORS)


def _redact_network_graph(graph: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Serve context-node labels redacted; returns the graph and how many labels changed."""
    redacted_nodes = []
    changed = 0
    for node in graph.get("nodes", []):
        stored = node.get("label_redacted")
        node = {k: v for k, v in node.items() if k != "label_redacted"}
        if node.get("type") == "context":
            original_label = node.get("label") or ""
            node["label"] = redaction.serve(original_label, stored)
            changed += node["label"] != original_label
        redacted_nodes.append(node)
    return {**graph, "nodes": redacted_nodes}, changed


_CHAT_SOURCE_FIELDS = (
    "platform",
    "channel_id",
    "channel_name",
    "url",
    "first_message_at",
    "last_message_at",
)


def _source_metadata(source: dict[str, Any]) -> dict[str, Any]:
    """What a reader may know about a source: where and when, never its text."""
    served = {
        k: source.get(k) for k in ("event_id", "source_platform", "source_type", "happened_at")
    }
    author = source.get("author") or {}
    served["author"] = {"display_name": author.get("display_name")} if author else None
    chat = source.get("chat_source")
    if isinstance(chat, dict):
        served["chat_source"] = {
            **{k: chat.get(k) for k in _CHAT_SOURCE_FIELDS},
            "message_count": len(chat.get("message_ids") or []),
            "discussion_message_count": len(chat.get("discussion_message_ids") or []),
        }
    return served


def _redact_node_details(details: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Serve a get_node_details response redacted; returns it and how many fields changed.

    ``summary`` comes from the stored redaction. Sources carry metadata only: source text
    is read through the audited admin raw view.
    """
    node_type = details.get("type")
    redacted = {k: v for k, v in details.items() if k != "summary_redacted"}
    changed = 0

    if node_type == "symbol":
        contexts, changed = redaction.serve_field(details.get("contexts", []))
        redacted["contexts"] = [
            {**c, "summary_redacted": c["summary"] != (o.get("summary") or "")}
            for c, o in zip(contexts, details.get("contexts", []), strict=True)
        ]

    elif node_type == "context":
        original_summary = details.get("summary") or ""
        new_summary = redaction.serve(original_summary, details.get("summary_redacted"))
        summary_was_redacted = new_summary != original_summary
        changed += summary_was_redacted
        redacted["summary"] = new_summary
        redacted["summary_redacted"] = summary_was_redacted
        redacted["sources"] = [_source_metadata(s) for s in details.get("sources", [])]

    return redacted, changed


def node_record_ids(details: dict[str, Any]) -> list[str]:
    """Ids of the records whose content a node-details response carries."""
    node_type = details.get("type")
    if node_type == "symbol":
        return [c["id"] for c in details.get("contexts", [])]
    if node_type == "context":
        _, _, context_id = str(details["id"]).partition(":")
        return [context_id, *(s["event_id"] for s in details.get("sources", []))]
    return []


@router.get("/api/v1/graph/context", response_model=ContextLookupResponse)
async def get_graph_context(
    symbol: str = Query(..., description="The symbol name to look up"),
    file_path: str | None = Query(None, description="Optional file path to restrict the lookup"),
    principal: Principal = Depends(get_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> ContextLookupResponse:
    """Retrieve active canonical contexts associated with a specific code symbol."""
    tenant_id = principal.tenant_id
    logger.info(
        "graph_context.lookup",
        tenant_id=str(tenant_id),
        symbol=symbol,
        file_path=file_path,
    )

    async with tenant_context(db_engine, tenant_id, principal.identity) as conn:
        store = PostgresEventStore(conn)
        contexts = await store.get_context_for_symbol(
            tenant_id=tenant_id,
            symbol=symbol,
            file_path=file_path,
        )
        contexts, redacted = redaction.serve_field(contexts)
        await audit.record(
            conn,
            tenant_id,
            principal.as_actor(),
            "graph.context",
            params={"symbol": symbol, "path": file_path},
            record_ids=[c["id"] for c in contexts],
            redacted=redacted,
            result_count=len(contexts),
        )

    return ContextLookupResponse(
        contexts=[
            ContextSummary(summary=c["summary"], confidence_score=c["confidence_score"])
            for c in contexts
        ]
    )


@router.get("/api/v1/graph/impact", response_model=ImpactWarningHeader)
async def get_graph_impact(
    symbol: str = Query(..., description="The symbol name to assess impact for"),
    principal: Principal = Depends(get_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> ImpactWarningHeader:
    """Retrieve all canonical contexts linked to a symbol and format them as a Warning Header."""
    tenant_id = principal.tenant_id
    logger.info(
        "graph_impact.lookup",
        tenant_id=str(tenant_id),
        symbol=symbol,
    )

    async with tenant_context(db_engine, tenant_id, principal.identity) as conn:
        store = PostgresEventStore(conn)
        contexts = await store.get_context_for_symbol(
            tenant_id=tenant_id,
            symbol=symbol,
        )
        contexts, redacted = redaction.serve_field(contexts)
        await audit.record(
            conn,
            tenant_id,
            principal.as_actor(),
            "graph.impact",
            params={"symbol": symbol},
            record_ids=[c["id"] for c in contexts],
            redacted=redacted,
            result_count=len(contexts),
        )

    if not contexts:
        warning_header = ""
    else:
        header_lines = [
            f"### WARNING: Active Design Constraints for symbol '{symbol}'",
            "The following high-confidence constraints or contexts were found:",
        ]
        for c in contexts:
            summary = c["summary"]
            score = c["confidence_score"]
            header_lines.append(f"- **[Confidence: {score:.1f}]** {summary}")
        warning_header = "\n".join(header_lines)

    return ImpactWarningHeader(
        symbol=symbol,
        warning_header=warning_header,
    )


@router.get("/api/v1/graph/decisions/active", response_model=ActiveDecisionsResponse)
async def get_graph_active_decisions(
    repo_identifier: str = Query(..., description="The repository identifier to query"),
    principal: Principal = Depends(get_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> ActiveDecisionsResponse:
    """The decision records in scope for a repository: blocking first, then by score."""
    tenant_id = principal.tenant_id
    logger.info(
        "graph_decisions.lookup",
        tenant_id=str(tenant_id),
        repo_identifier=repo_identifier,
    )

    async with tenant_context(db_engine, tenant_id, principal.identity) as conn:
        store = PostgresEventStore(conn)
        contexts = await store.get_top_contexts_for_repo(
            tenant_id=tenant_id,
            repo_identifier=repo_identifier,
            limit=10,
            decisions_only=True,
        )
        contexts, redacted = redaction.serve_field(contexts)
        await audit.record(
            conn,
            tenant_id,
            principal.as_actor(),
            "graph.active_decisions",
            params={"repo": repo_identifier},
            record_ids=[c["id"] for c in contexts],
            redacted=redacted,
            result_count=len(contexts),
        )

    if not contexts:
        markdown_content = ""
    else:
        header_lines = [
            f"# Architectural Decisions for {repo_identifier}",
            "The following decisions are active for this repository.",
            "",
        ]
        for c in contexts:
            summary = c["summary"]
            score = c["confidence_score"]
            header_lines.append(f"## [Confidence: {score:.1f}]")
            header_lines.append(f"{summary}")
            header_lines.append("")

        markdown_content = "\n".join(header_lines)

    return ActiveDecisionsResponse(
        repo_identifier=repo_identifier,
        markdown_content=markdown_content.strip(),
    )


async def process_ast_sync(
    tenant_id: UUID,
    repo_identifier: str,
    nodes: list[ASTNodeSyncItem],
    db_engine: AsyncEngine,
) -> None:
    """Background task to process AST node upsert and drift checks in bulk."""
    logger.info(
        "ast_sync.process_start",
        tenant_id=str(tenant_id),
        repo_identifier=repo_identifier,
        node_count=len(nodes),
    )
    try:
        async with tenant_context(db_engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            for item in nodes:
                await store.upsert_ast_node_and_check_drift(
                    tenant_id=tenant_id,
                    repo_identifier=repo_identifier,
                    file_path=item.file_path,
                    symbol_name=item.symbol_name,
                    node_type=item.node_type,
                    merkle_hash=item.merkle_hash,
                )
        await code_links.link_repository(db_engine, tenant_id, repo_identifier)
        logger.info(
            "ast_sync.process_complete",
            tenant_id=str(tenant_id),
            repo_identifier=repo_identifier,
        )
    except Exception as e:
        logger.error(
            "ast_sync.process_failed",
            tenant_id=str(tenant_id),
            repo_identifier=repo_identifier,
            error=str(e),
            exc_info=True,
        )


@router.post("/api/v1/graph/ast/sync", response_model=ASTSyncResponse, status_code=202)
async def sync_ast(
    payload: ASTSyncPayload,
    background_tasks: BackgroundTasks,
    tenant_id: UUID = Depends(verify_push_token),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> ASTSyncResponse:
    """Ingest updated Merkle-AST states directly from an IDE or Git hook."""
    logger.info(
        "ast_sync.request_received",
        tenant_id=str(tenant_id),
        repo_identifier=payload.repo_identifier,
        node_count=len(payload.nodes),
    )

    background_tasks.add_task(
        process_ast_sync,
        tenant_id=tenant_id,
        repo_identifier=payload.repo_identifier,
        nodes=payload.nodes,
        db_engine=db_engine,
    )

    return ASTSyncResponse(
        status="accepted",
        message="AST node sync request accepted and enqueued for processing.",
    )


@router.get(
    "/api/v1/graph/repositories",
    response_model=GraphRepositories,
    summary="Repositories with a code graph",
)
async def list_graph_repositories(
    principal: Principal = Depends(get_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> GraphRepositories:
    """Names and file counts only, for choosing a graph to open."""
    async with tenant_context(db_engine, principal.tenant_id, principal.identity) as conn:
        found = await PostgresEventStore(conn).list_graph_repositories(principal.tenant_id)
    return GraphRepositories(items=[GraphRepository(repo_identifier=r, files=n) for r, n in found])


@router.get("/api/v1/graph/network", response_model=NetworkGraphResponse)
async def get_network_graph(
    repo_identifier: str = Query(..., description="The repository identifier to query"),
    principal: Principal = Depends(get_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> NetworkGraphResponse:
    """Retrieve the complete network graph representing Files, Symbols, Contexts, and Developers."""
    tenant_id = principal.tenant_id
    logger.info(
        "network_graph.lookup",
        tenant_id=str(tenant_id),
        repo_identifier=repo_identifier,
    )
    async with tenant_context(db_engine, tenant_id, principal.identity) as conn:
        store = PostgresEventStore(conn)
        graph = await store.get_network_graph(
            tenant_id=tenant_id,
            repo_identifier=repo_identifier,
        )
        graph, redacted = _redact_network_graph(graph)
        contexts = [n for n in graph.get("nodes", []) if n.get("type") == "context"]
        await audit.record(
            conn,
            tenant_id,
            principal.as_actor(),
            "graph.network",
            params={"repo": repo_identifier},
            record_ids=[str(n["id"]).partition(":")[2] for n in contexts],
            redacted=redacted,
            result_count=len(contexts),
        )
    return NetworkGraphResponse(**graph)


@router.get("/api/v1/graph/node/{node_id:path}", response_model=NodeDetailsResponse)
async def get_node_details(
    node_id: str,
    repo_identifier: str = Query(..., description="The repository identifier context"),
    principal: Principal = Depends(get_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> NodeDetailsResponse:
    """Retrieve details for a single graph node by node_id."""
    tenant_id = principal.tenant_id
    logger.info(
        "node_details.lookup",
        tenant_id=str(tenant_id),
        node_id=node_id,
        repo_identifier=repo_identifier,
    )
    async with tenant_context(db_engine, tenant_id, principal.identity) as conn:
        store = PostgresEventStore(conn)
        details = await store.get_node_details(
            tenant_id=tenant_id,
            node_id=node_id,
            repo_identifier=repo_identifier,
        )
        if details:
            details, redacted = _redact_node_details(details)
            if details["type"] == "context":
                record_id = str(details["id"]).partition(":")[2]
                found = await record_notes.updates(conn, tenant_id, [record_id])
                details["updates"] = found.get(record_id, [])
            await audit.record(
                conn,
                tenant_id,
                principal.as_actor(),
                "graph.node",
                params={"node": node_id, "repo": repo_identifier},
                record_ids=node_record_ids(details),
                redacted=redacted,
                result_count=1,
            )

    if not details:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=f"Node {node_id} not found or access denied")

    return NodeDetailsResponse(**details)


@records_router.patch(
    "/api/v1/admin/records/{context_id}/scope",
    response_model=ScopeUpdateResponse,
    summary="Replace a record's scope",
)
async def update_context_scope(
    context_id: UUID,
    payload: ScopeUpdateRequest,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> ScopeUpdateResponse:
    """Replace a canonical context's scope set on a human's instruction.

    Every change is recorded in ``context_scope_audit_log`` with the prior
    values, the actor and the time. ``scope_rows`` and ``repo_identifiers``
    both replace the record's whole scope set; this is not an
    add-or-remove-one-entry API.
    """
    tenant_id = principal.tenant_id
    scope_rows_payload = (
        [
            {"scope_type": row.scope_type, "scope_value": row.scope_value}
            for row in payload.scope_rows
        ]
        if payload.scope_rows is not None
        else None
    )
    logger.info(
        "context_scope.update_requested",
        tenant_id=str(tenant_id),
        context_id=str(context_id),
        new_repo_identifiers=payload.repo_identifiers,
        new_scope_rows=scope_rows_payload,
        new_is_locked=payload.is_locked,
        changed_by=principal.actor,
    )
    async with tenant_context(db_engine, tenant_id) as conn:
        store = PostgresEventStore(conn)
        updated = await store.update_context_scope(
            tenant_id=tenant_id,
            canonical_id=context_id,
            repo_identifiers=payload.repo_identifiers,
            is_locked=payload.is_locked,
            changed_by=principal.actor,
            scope_rows=scope_rows_payload,
        )
        if updated is not None:
            await audit.record(
                conn,
                tenant_id,
                principal.as_actor(),
                "context.scope_changed",
                params={
                    "repo_identifiers": payload.repo_identifiers,
                    "scope_rows": scope_rows_payload,
                    "is_locked": payload.is_locked,
                },
                record_ids=[context_id],
            )

    if updated is None:
        raise HTTPException(
            status_code=404, detail=f"Context {context_id} not found or access denied"
        )

    return ScopeUpdateResponse(
        id=str(updated["id"]),
        scope_repo_identifiers=updated["scope_repo_identifiers"],
        scope_rows=[ScopeRow(**row) for row in updated["scope_rows"]],
        is_locked=updated["is_locked"],
    )


@records_router.patch(
    "/api/v1/admin/records/{context_id}/severity",
    response_model=SeverityUpdateResponse,
    summary="Set a record's severity",
)
async def update_context_severity(
    context_id: UUID,
    payload: SeverityUpdateRequest,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> SeverityUpdateResponse:
    """Set whether a record fails the pull-request check (blocking) or is only cited."""
    changed = await records.set_severity(
        db_engine, principal.tenant_id, context_id, payload.severity, principal.as_actor()
    )
    if not changed:
        raise HTTPException(
            status_code=404, detail=f"Context {context_id} not found or access denied"
        )
    return SeverityUpdateResponse(id=str(context_id), severity=payload.severity)


@records_router.patch(
    "/api/v1/admin/records/{context_id}/type",
    response_model=TypeUpdateResponse,
    summary="Set a record's type",
)
async def update_context_type(
    context_id: UUID,
    payload: TypeUpdateRequest,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> TypeUpdateResponse:
    """Make a record a decision (adr, constraint, business_requirement, risk) or reference."""
    changed = await records.set_type(
        db_engine, principal.tenant_id, context_id, payload.type, principal.as_actor()
    )
    if not changed:
        raise HTTPException(
            status_code=404, detail=f"Context {context_id} not found or access denied"
        )
    return TypeUpdateResponse(id=str(context_id), type=payload.type)


@router.get("/api/v1/graph/context/{context_id}/scope/history", response_model=ScopeHistoryResponse)
async def get_context_scope_history(
    context_id: UUID,
    principal: Principal = Depends(get_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> ScopeHistoryResponse:
    """Most recent scope or is_locked change for a record.

    Shown as "last changed by X at Y" before an edit, so a concurrent edit is
    at least visible. There is no optimistic locking.
    """
    tenant_id = principal.tenant_id
    async with tenant_context(db_engine, tenant_id, principal.identity) as conn:
        readable = await conn.scalar(
            select(CanonicalContext.id).where(
                (CanonicalContext.tenant_id == tenant_id) & (CanonicalContext.id == context_id)
            )
        )
        if readable is None:
            raise HTTPException(
                status_code=404, detail=f"Context {context_id} not found or access denied"
            )
        store = PostgresEventStore(conn)
        latest = await store.get_latest_scope_change(tenant_id, context_id)

    return ScopeHistoryResponse(
        changed_by=latest["changed_by"] if latest else None,
        changed_at=latest["changed_at"] if latest else None,
    )


@router.get("/api/v1/graph/ping")
async def ping_graph(
    repo_identifier: str = Query(..., description="The repository identifier to verify access"),
    principal: Principal = Depends(get_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, str]:
    """Verify that the user's tenant has access to the specified repository (lightweight check)."""
    tenant_id = principal.tenant_id
    logger.info(
        "graph_ping",
        tenant_id=str(tenant_id),
        repo_identifier=repo_identifier,
    )
    async with tenant_context(db_engine, tenant_id, principal.identity) as conn:
        store = PostgresEventStore(conn)
        has_access = await store.check_repo_access(tenant_id, repo_identifier)

    if not has_access:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=403,
            detail="You may not view this repository's graph",
        )

    return {"status": "ok", "repo_identifier": repo_identifier}
