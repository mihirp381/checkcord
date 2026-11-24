"""Zoom recording webhooks for meeting transcripts."""

import hashlib
import hmac
import json
import time
from uuid import UUID

import aiohttp
import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.meetings.parser import parse_and_chunk_transcript
from checkcord.api import rate_limit
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core.crypto import decrypt_secret
from checkcord.models.enums import SourcePlatform
from checkcord.services.connections import connection_key
from checkcord.storage.models import ZoomInstallation

logger = structlog.get_logger(__name__).bind(component="zoom_webhook")

router = APIRouter()


async def get_db_engine(request: Request) -> AsyncEngine | None:
    ctx = getattr(request.app.state, "ctx", None)
    if ctx and hasattr(ctx, "db_engine") and ctx.db_engine:
        from typing import cast

        return cast(AsyncEngine, ctx.db_engine)
    return getattr(request.app.state, "db_engine", None)


async def download_transcript(url: str, download_token: str | None = None) -> str:
    """Download VTT transcript content from Zoom download URL, with the webhook's
    download token as a bearer token."""
    headers = {"Authorization": f"Bearer {download_token}"} if download_token else {}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to download transcript from Zoom: HTTP {resp.status}",
                )
            return str(await resp.text())


MAX_TIMESTAMP_SKEW_SECONDS = 300


def _timestamp_is_fresh(timestamp: str, now: float | None = None) -> bool:
    try:
        sent = int(timestamp)
    except ValueError:
        return False
    current = time.time() if now is None else now
    return abs(current - sent) <= MAX_TIMESTAMP_SKEW_SECONDS


@router.post("/api/v1/webhooks/zoom", status_code=202)
async def zoom_webhook(
    request: Request,
    zoom_account_id: str | None = None,
    tenant_id: str | None = None,
) -> Response:
    """FastAPI endpoint to handle Zoom webhook events."""
    body_bytes = await request.body()
    try:
        body_json = json.loads(body_bytes) if body_bytes else {}
    except Exception as e:
        logger.error("malformed_json_payload", error=str(e))
        raise HTTPException(status_code=400, detail="Malformed JSON payload") from e

    event_type = body_json.get("event")

    # Resolve Installation / Tenant
    # Search in order: query parameter zoom_account_id, query parameter tenant_id,
    # then payload account_id
    search_acc_id = zoom_account_id
    if not search_acc_id and event_type != "endpoint.url_validation":
        search_acc_id = body_json.get("payload", {}).get("account_id")

    db_engine = await get_db_engine(request)
    if not db_engine:
        raise HTTPException(status_code=500, detail="Database engine not initialized")

    # Query ZoomInstallation globally (bypass RLS for routing lookup)
    async with db_engine.connect() as conn:
        from sqlalchemy import text

        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))

        from sqlalchemy import select

        if search_acc_id:
            stmt = select(ZoomInstallation).where(ZoomInstallation.zoom_account_id == search_acc_id)
        elif tenant_id:
            try:
                t_uuid = UUID(tenant_id)
                stmt = select(ZoomInstallation).where(ZoomInstallation.tenant_id == t_uuid)
            except ValueError as val_err:
                raise HTTPException(
                    status_code=400, detail="Invalid tenant_id UUID format"
                ) from val_err
        else:
            logger.warning("unresolvable_zoom_webhook_tenant")
            raise HTTPException(
                status_code=400, detail="Unable to resolve Zoom installation tenant context"
            )

        res = await conn.execute(stmt)
        installation = res.mappings().fetchone()

    if not installation:
        logger.warning(
            "zoom_installation_not_found", search_acc_id=search_acc_id, tenant_id=tenant_id
        )
        raise HTTPException(status_code=401, detail="Unauthorized: Zoom installation not found")

    resolved_tenant_id: UUID = installation["tenant_id"]
    webhook_secret = decrypt_secret(installation["webhook_secret"])

    # Validate Signature
    timestamp = request.headers.get("x-zm-request-timestamp")
    signature = request.headers.get("x-zm-signature")

    if not signature or not timestamp:
        logger.warning("missing_zoom_signature_headers")
        raise HTTPException(status_code=401, detail="Unauthorized: Missing signature or timestamp")

    if not _timestamp_is_fresh(timestamp):
        logger.warning("stale_zoom_webhook_timestamp", search_acc_id=search_acc_id)
        raise HTTPException(status_code=401, detail="Unauthorized: Stale or invalid timestamp")

    message = f"v0:{timestamp}:{body_bytes.decode('utf-8')}"
    expected = (
        "v0="
        + hmac.new(
            webhook_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        ).hexdigest()
    )

    if not hmac.compare_digest(expected, signature):
        logger.warning("invalid_zoom_webhook_signature", search_acc_id=search_acc_id)
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid signature")

    await rate_limit.webhook(request, connection_key("zoom", installation["zoom_account_id"]))

    # Handle URL Validation Challenge
    if event_type == "endpoint.url_validation":
        plain_token = body_json.get("payload", {}).get("plainToken")
        if not plain_token:
            raise HTTPException(status_code=400, detail="Missing plainToken for URL validation")

        encrypted_token = hmac.new(
            webhook_secret.encode("utf-8"), plain_token.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        return Response(
            content=json.dumps({"plainToken": plain_token, "encryptedToken": encrypted_token}),
            media_type="application/json",
            status_code=200,
        )

    # Handle Recording/Transcript Completed
    if event_type in ("recording.completed", "recording.transcript_completed"):
        meeting_obj = body_json.get("payload", {}).get("object", {})
        meeting_id = str(meeting_obj.get("id"))
        meeting_uuid = meeting_obj.get("uuid")
        topic = meeting_obj.get("topic", "Zoom Meeting")
        start_time_str = meeting_obj.get("start_time")
        host_email = meeting_obj.get("host_email") or "unknown@zoom.local"

        # Determine meeting start datetime
        if start_time_str:
            from datetime import UTC, datetime

            # Handle Z suffix for compatibility
            parsed_str = start_time_str
            if parsed_str.endswith("Z"):
                parsed_str = parsed_str[:-1] + "+00:00"
            meeting_start = datetime.fromisoformat(parsed_str)
            if meeting_start.tzinfo is None:
                meeting_start = meeting_start.replace(tzinfo=UTC)
        else:
            from datetime import UTC, datetime

            meeting_start = datetime.now(UTC)

        # Locate transcript file
        files = meeting_obj.get("recording_files", [])
        transcript_file = None
        for f in files:
            if f.get("file_type") == "TRANSCRIPT" and f.get("status") == "completed":
                transcript_file = f
                break

        if not transcript_file:
            logger.info("no_completed_transcript_found_in_zoom_payload", meeting_id=meeting_id)
            return Response(
                content=json.dumps({"status": "no_transcript"}),
                media_type="application/json",
                status_code=200,
            )

        download_url = transcript_file.get("download_url")
        if not download_url:
            logger.warning("transcript_missing_download_url", meeting_id=meeting_id)
            raise HTTPException(status_code=400, detail="Transcript file missing download URL")

        # Download VTT
        try:
            vtt_content = await download_transcript(download_url, body_json.get("download_token"))
        except Exception as e:
            detail = e.detail if isinstance(e, HTTPException) else type(e).__name__
            logger.warning(
                "failed_to_download_zoom_transcript", meeting_id=meeting_id, error=detail
            )
            raise HTTPException(
                status_code=500, detail=f"Failed to fetch VTT file: {detail}"
            ) from e

        duration_minutes = meeting_obj.get("duration")
        duration_seconds = (
            int(duration_minutes * 60) if isinstance(duration_minutes, int | float) else None
        )
        has_recording = any(f.get("file_type") in ("MP4", "M4A", "VIDEO", "AUDIO") for f in files)

        # Parse & Chunk into CanonicalEvents
        events = await parse_and_chunk_transcript(
            vtt_text=vtt_content,
            db_engine=db_engine,
            tenant_id=resolved_tenant_id,
            meeting_id=meeting_uuid or meeting_id,
            meeting_topic=topic,
            meeting_start=meeting_start,
            host_email_or_name=host_email,
            platform=SourcePlatform.ZOOM,
            duration_seconds=duration_seconds,
            has_recording=has_recording,
            connection=connection_key("zoom", installation["zoom_account_id"]),
        )

        if not events:
            logger.info("empty_zoom_transcript_ignored", meeting_id=meeting_id)
            return Response(
                content=json.dumps({"status": "empty_transcript_ignored"}),
                media_type="application/json",
                status_code=200,
            )

        # Publish events to Redis Stream
        ctx = getattr(request.app.state, "ctx", None)
        if not ctx or not ctx.redis_client:
            raise HTTPException(status_code=500, detail="Redis client not initialized")

        bus = RedisStreamsEventBus(ctx.redis_client)
        published_ids = []
        for event in events:
            msg_id = await bus.publish(topic="events.raw.meetings", event=event)
            published_ids.append(str(event.event_id))
            logger.info(
                "zoom_transcript_chunk_published", event_id=str(event.event_id), msg_id=msg_id
            )

        return Response(
            content=json.dumps(
                {
                    "status": "queued",
                    "transcript_ids": published_ids,
                }
            ),
            media_type="application/json",
            status_code=202,
        )

    # Ignore other events
    logger.info("ignored_zoom_webhook_event", event=event_type)
    return Response(
        content=json.dumps({"status": "ignored"}),
        media_type="application/json",
        status_code=200,
    )
