"""Agent ledger entries and skill search."""

import json
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.api.deps import Principal, get_db_engine, get_principal
from checkcord.api.push_router import verify_push_token
from checkcord.core import redaction
from checkcord.storage import audit
from checkcord.storage.postgres import PostgresEventStore, tenant_context

logger = structlog.get_logger(__name__).bind(component="agent_ledger_router")

router = APIRouter()

ALLOWED_SCOPES = {"user", "project", "repository"}
MAX_VALUE_BYTES = 1 * 1024 * 1024  # 1MB limit for values


class LedgerUpsertPayload(BaseModel):
    scope_type: str = Field(..., description="Scope type: user, project, or repository")
    scope_id: str = Field(
        ..., description="Identifier for the scope (e.g. email, project key, repo)"
    )
    key: str = Field(..., description="Key for the ledger entry")
    value: dict[str, Any] = Field(..., description="JSON value payload")
    agent_session_id: str | None = Field(None, description="Optional associated agent session ID")
    event_id: UUID | None = Field(None, description="Optional associated event ID")


class LedgerEntryResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    scope_type: str
    scope_id: str
    key: str
    value: dict[str, Any]
    agent_session_id: str | None = None
    event_id: UUID | None = None


class SkillResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str
    description: str
    instructions: str


@router.get("/api/v1/agents/ledger")
async def get_ledger(
    request: Request,
    scope_type: str | None = None,
    scope_id: str | None = None,
    key: str | None = None,
    principal: Principal = Depends(get_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> Any:
    """Query the agent ledger for entries or a specific key within a scope."""
    tenant_id = principal.tenant_id
    if scope_type and scope_type not in ALLOWED_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid scope_type '{scope_type}'. Must be one of {ALLOWED_SCOPES}",
        )

    async with tenant_context(db_engine, tenant_id) as conn:
        store = PostgresEventStore(conn)

        # If a specific key is requested, both scope_type and scope_id must be provided
        if key is not None:
            if not scope_type or not scope_id:
                raise HTTPException(
                    status_code=400,
                    detail="Querying a specific key requires both 'scope_type' and 'scope_id'",
                )

            entry = await store.get_ledger_entry(tenant_id, scope_type, scope_id, key)
            if entry is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Ledger key '{key}' not found under scope {scope_type}/{scope_id}",
                )
            entries = [entry]
        else:
            entries = await store.get_ledger_entries(tenant_id, scope_type, scope_id)
        await audit.record(
            conn,
            tenant_id,
            principal.as_actor(),
            "ledger.read",
            params={"scope_type": scope_type, "scope_id": scope_id, "key": key},
            record_ids=[e["id"] for e in entries],
            result_count=len(entries),
        )
    return entry if key is not None else entries


@router.post("/api/v1/agents/ledger", status_code=200)
async def upsert_ledger(
    payload: LedgerUpsertPayload,
    request: Request,
    tenant_id: UUID = Depends(verify_push_token),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, str]:
    """Upsert a stateful ledger entry under the resolved tenant context."""
    if payload.scope_type not in ALLOWED_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid scope_type '{payload.scope_type}'. Must be one of {ALLOWED_SCOPES}",
        )

    # Validate value size
    try:
        serialized_val = json.dumps(payload.value)
    except Exception as err:
        raise HTTPException(
            status_code=400,
            detail=f"Value is not JSON serializable: {err}",
        ) from err

    if len(serialized_val) > MAX_VALUE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Value payload too large: {len(serialized_val)} bytes "
                f"exceeds {MAX_VALUE_BYTES} byte limit"
            ),
        )

    async with tenant_context(db_engine, tenant_id) as conn:
        store = PostgresEventStore(conn)
        success = await store.upsert_ledger_entry(
            tenant_id=tenant_id,
            scope_type=payload.scope_type,
            scope_id=payload.scope_id,
            key=payload.key,
            value=payload.value,
            agent_session_id=payload.agent_session_id,
            event_id=payload.event_id,
        )

        if not success:
            raise HTTPException(
                status_code=500,
                detail="Failed to persist ledger entry",
            )

    logger.info(
        "agent_ledger_upserted",
        tenant_id=str(tenant_id),
        scope_type=payload.scope_type,
        scope_id=payload.scope_id,
        key=payload.key,
    )

    return {"status": "success"}


_SKILL_TEXT = ("name", "description", "instructions")


@router.get("/api/v1/agents/skills")
async def search_skills(
    request: Request,
    query: str | None = None,
    principal: Principal = Depends(get_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> Any:
    """Search synthesized skills by query using simple ILIKE pattern matching."""
    tenant_id = principal.tenant_id
    async with tenant_context(db_engine, tenant_id) as conn:
        store = PostgresEventStore(conn)
        skills = await store.search_skills(tenant_id, query)
        served = [
            {**s, **{f: redaction.redact_regex(s.get(f)) for f in _SKILL_TEXT if s.get(f)}}
            for s in skills
        ]
        redacted = sum(a != b for a, b in zip(served, skills, strict=True))
        skills = served
        await audit.record(
            conn,
            tenant_id,
            principal.as_actor(),
            "skills.read",
            params={"query": audit.search_text(query)},
            record_ids=[s["id"] for s in skills],
            redacted=redacted,
            result_count=len(skills),
        )
    return skills
