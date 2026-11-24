"""Agent session transcripts in."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from checkcord.adapters.ingestion.agent_parser import parse_trajectory
from checkcord.api.push_router import verify_push_token
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import (
    AccessVisibility,
    AclType,
    SourcePlatform,
    SourceType,
)
from checkcord.models.event import CanonicalEvent
from checkcord.schemas.agent import AgentTrajectoryPayload

logger = structlog.get_logger(__name__).bind(component="agent_router")

router = APIRouter()

# 10 MB payload limit
MAX_PAYLOAD_BYTES = 10 * 1024 * 1024


@router.post("/api/v1/agents/trajectory", status_code=202)
async def ingest_agent_trajectory(
    request: Request,
    tenant_id: UUID = Depends(verify_push_token),
) -> dict[str, str | list[str]]:
    """Ingest an agent session trajectory and publish to Redis.

    Accepts structured JSONL steps or raw transcript text. Extracts
    tool invocations and file references, then publishes as a
    CanonicalEvent to the `events.raw.agent` stream.
    """
    # Enforce payload size limit
    body_bytes = await request.body()
    if len(body_bytes) > MAX_PAYLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Payload too large: {len(body_bytes)} bytes exceeds {MAX_PAYLOAD_BYTES} byte limit"
            ),
        )

    # Parse and validate payload
    try:
        payload = AgentTrajectoryPayload.model_validate_json(body_bytes)
    except Exception as e:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid payload: {e}",
        ) from e

    # Require at least one transcript source
    if not payload.steps and not payload.raw_transcript:
        raise HTTPException(
            status_code=422,
            detail=("At least one of 'steps' or 'raw_transcript' must be provided"),
        )

    # Parse trajectory
    content_raw, content_clean, entity_refs, extraction_meta = parse_trajectory(payload)

    event_id = payload.event_id or uuid4()
    session_id = payload.session_id or str(event_id)
    happened_at = payload.happened_at or datetime.now(UTC)

    agent_name = payload.agent_name or "unknown-agent"

    author = AuthorInfo(
        source_id=session_id,
        display_name=agent_name,
    )

    access_control = AccessControl(
        visibility=AccessVisibility.PRIVATE,
        acl_type=AclType.CUSTOM,
        is_public=False,
    )

    # Build event metadata
    event_metadata: dict[str, object] = dict(payload.metadata)
    event_metadata["agent_name"] = agent_name
    event_metadata["session_id"] = session_id
    if payload.repository:
        event_metadata["repository"] = payload.repository
    if payload.task_description:
        event_metadata["task_description"] = payload.task_description
    event_metadata["tool_invocations"] = extraction_meta["tool_invocations"]
    event_metadata["file_paths"] = extraction_meta["file_paths"]

    try:
        event = CanonicalEvent(
            event_id=event_id,
            tenant_id=tenant_id,
            source_platform=SourcePlatform.AGENT_SESSION,
            source_type=SourceType.AGENT_LOG,
            source_id=session_id,
            happened_at=happened_at,
            author=author,
            content_raw=content_raw,
            content_clean=content_clean,
            access_control=access_control,
            entities=entity_refs,
            metadata=event_metadata,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    # Publish to dedicated agent stream
    ctx = getattr(request.app.state, "ctx", None)
    if not ctx or not ctx.redis_client:
        raise HTTPException(
            status_code=500,
            detail="Redis client not initialized",
        )

    bus = RedisStreamsEventBus(ctx.redis_client)
    msg_id = await bus.publish("events.raw.agent", event)

    logger.info(
        "agent_trajectory_ingested",
        event_id=str(event.event_id),
        msg_id=msg_id,
        tool_count=len(extraction_meta["tool_invocations"]),
        file_count=len(extraction_meta["file_paths"]),
        entity_ref_count=len(entity_refs),
    )

    return {
        "status": "accepted",
        "event_id": str(event.event_id),
        "tools_extracted": extraction_meta["tool_invocations"],
        "files_referenced": extraction_meta["file_paths"],
    }
