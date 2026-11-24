from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.api.deps import (
    Principal,
    get_db_engine,
    require_admin_principal,
    verify_hybrid_bearer_token,
)
from checkcord.api.schemas import ERRORS
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import AccessVisibility, AclType
from checkcord.models.event import CanonicalEvent
from checkcord.schemas.push import ACLOverridePayload, CustomPushPayload
from checkcord.services.connections import PUSH_CONNECTION
from checkcord.storage import audit
from checkcord.storage.postgres import PostgresEventStore, tenant_context

router = APIRouter(responses=ERRORS)


async def verify_push_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> UUID:
    """Verify the push API Bearer token, delegating to the shared hybrid verifier."""
    tenant_id: UUID = await verify_hybrid_bearer_token(request, authorization)
    return tenant_id


@router.post("/api/v1/ingest/custom", status_code=201, tags=["ingest"], summary="Push an event")
async def ingest_custom_data(
    payload: CustomPushPayload,
    request: Request,
    tenant_id: UUID = Depends(verify_push_token),
) -> dict[str, UUID]:
    """Ingest custom event payload, construct a CanonicalEvent, and publish to Redis Streams."""
    event_id = payload.event_id or uuid4()
    source_id = payload.source_id or str(event_id)
    happened_at = payload.happened_at or datetime.now(UTC)

    author = payload.author or AuthorInfo(source_id=source_id, display_name="Custom Ingest")
    content_clean = payload.content_clean or payload.content_raw

    access_control = payload.access_control or AccessControl(
        visibility=AccessVisibility.PRIVATE,
        acl_type=AclType.CUSTOM,
        is_public=False,
    )

    try:
        event = CanonicalEvent(
            event_id=event_id,
            tenant_id=tenant_id,
            parent_event_id=payload.parent_event_id,
            fingerprint=payload.fingerprint or "",
            schema_version=payload.schema_version,
            source_platform=payload.source_platform,
            source_type=payload.source_type,
            event_action=payload.event_action,
            source_id=source_id,
            version=payload.version,
            happened_at=happened_at,
            author=author,
            content_raw=payload.content_raw,
            content_clean=content_clean,
            source_raw_payload=payload.source_raw_payload,
            access_control=access_control,
            signal_status=payload.signal_status,
            code_metadata=payload.code_metadata,
            task_metadata=payload.task_metadata,
            document_metadata=payload.document_metadata,
            entities=payload.entities,
            metadata=payload.metadata,
            connection=PUSH_CONNECTION,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    ctx = getattr(request.app.state, "ctx", None)
    if not ctx or not ctx.redis_client:
        raise HTTPException(status_code=500, detail="Redis client not initialized")

    bus = RedisStreamsEventBus(ctx.redis_client)
    # events.raw.chat takes RawChatPayload only; custom events go to their own topic.
    await bus.publish("events.raw.custom", event)

    return {"event_id": event.event_id}


@router.post(
    "/api/v1/admin/events/{event_id}/acl-override",
    status_code=200,
    tags=["admin: events"],
    summary="Override an event's audience",
)
async def override_acl(
    event_id: UUID,
    payload: ACLOverridePayload,
    request: Request,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, str]:
    """Override Access Control admin list for a specific event under resolved tenant."""
    tenant_id = principal.tenant_id
    async with tenant_context(db_engine, tenant_id) as conn:
        store = PostgresEventStore(conn)
        updated = await store.update_admin_overrides(tenant_id, event_id, payload.admin_overrides)
        if not updated:
            raise HTTPException(status_code=404, detail="Event not found")
        await store.recompute_access_for_event(tenant_id, event_id)
        await audit.record(
            conn,
            tenant_id,
            principal.as_actor(),
            "event.audience_override",
            params={"admin_overrides": payload.admin_overrides},
            record_ids=[event_id],
        )

    return {"status": "success"}
