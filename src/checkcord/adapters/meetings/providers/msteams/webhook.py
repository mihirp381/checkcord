"""Microsoft Graph notifications for new Teams meeting transcripts."""

import hmac
import json
import re
from typing import Any, cast

import aiohttp
import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.chat.providers.msteams.graph_token import get_msteams_access_token
from checkcord.adapters.meetings.parser import parse_and_chunk_transcript
from checkcord.adapters.meetings.providers.msteams import subscriptions
from checkcord.api import rate_limit
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core import http_retry
from checkcord.core.config import get_settings
from checkcord.core.crypto import decrypt_secret
from checkcord.models.enums import SourcePlatform
from checkcord.services.connections import connection_key
from checkcord.storage.models import MsTeamsMeetingInstallation
from checkcord.storage.postgres import app_bypass_rls_context

logger = structlog.get_logger(__name__).bind(component="msteams_meeting_webhook")

router = APIRouter()

_KEY_SEGMENT = re.compile(r"\('([^'/]*)'\)")


async def get_db_engine(request: Request) -> AsyncEngine | None:
    ctx = getattr(request.app.state, "ctx", None)
    if ctx and hasattr(ctx, "db_engine") and ctx.db_engine:
        return cast(AsyncEngine, ctx.db_engine)
    return getattr(request.app.state, "db_engine", None)


async def fetch_graph_api(url: str, token: str) -> dict[str, Any]:
    """Fetch JSON data from Microsoft Graph API."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with aiohttp.ClientSession() as session:
        answer = await http_retry.aiohttp_json(
            session, "GET", url, provider="microsoft", headers=headers
        )
    if answer.status != 200:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch Graph API details: HTTP {answer.status} - {answer.text}",
        )
    return cast(dict[str, Any], answer.body)


async def fetch_graph_transcript_content(url: str, token: str) -> str:
    """Fetch WebVTT transcript content from Microsoft Graph API."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with aiohttp.ClientSession() as session:
        answer = await http_retry.aiohttp_json(
            session, "GET", url, provider="microsoft", as_json=False, headers=headers
        )
    if answer.status != 200:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch Graph transcript: HTTP {answer.status} - {answer.text}",
        )
    return answer.text


async def _installation(db_engine: AsyncEngine, ms_tenant_id: str) -> Any:
    async with app_bypass_rls_context(db_engine) as conn:
        res = await conn.execute(
            select(MsTeamsMeetingInstallation).where(
                MsTeamsMeetingInstallation.ms_tenant_id == ms_tenant_id
            )
        )
        return res.mappings().fetchone()


def _client_state_ok(installation: Any, client_state: Any) -> bool:
    expected = decrypt_secret(installation["webhook_secret"])
    return bool(client_state) and hmac.compare_digest(str(client_state).encode(), expected.encode())


@router.post(subscriptions.LIFECYCLE_PATH, status_code=202)
async def msteams_meeting_lifecycle(request: Request) -> Response:
    """Graph lifecycle notifications: renew, or create again, the transcript subscription."""
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        return Response(content=validation_token, media_type="text/plain", status_code=200)
    try:
        notifications = json.loads(await request.body() or b"{}").get("value", [])
    except (ValueError, AttributeError) as e:
        raise HTTPException(status_code=400, detail="Malformed JSON payload") from e
    db_engine = await get_db_engine(request)
    if not db_engine:
        raise HTTPException(status_code=500, detail="Database engine not initialized")
    for note in notifications:
        ms_tenant_id, event = note.get("tenantId"), note.get("lifecycleEvent")
        if not ms_tenant_id:
            continue
        installation = await _installation(db_engine, ms_tenant_id)
        if installation is None or not _client_state_ok(installation, note.get("clientState")):
            logger.warning("invalid_msteams_lifecycle_notification", ms_tenant_id=ms_tenant_id)
            continue
        await rate_limit.webhook(request, connection_key("teams-meetings", ms_tenant_id))
        logger.info("msteams_lifecycle_notification", lifecycle_event=event)
        if event in ("reauthorizationRequired", "subscriptionRemoved"):
            await subscriptions.renew_for(
                db_engine, ms_tenant_id, recreate=event == "subscriptionRemoved"
            )
    return Response(status_code=202)


@router.post("/api/v1/webhooks/msteams/meetings", status_code=202)
async def msteams_meeting_webhook(request: Request) -> Response:
    """FastAPI endpoint to receive Microsoft Graph change notifications."""
    # Handle validation challenge from Microsoft
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        return Response(content=validation_token, media_type="text/plain", status_code=200)

    body_bytes = await request.body()
    try:
        body_json = json.loads(body_bytes) if body_bytes else {}
    except Exception as e:
        logger.error("malformed_json_payload", error=str(e))
        raise HTTPException(status_code=400, detail="Malformed JSON payload") from e

    notifications = body_json.get("value", [])
    if not notifications:
        return Response(
            content=json.dumps({"status": "no_notifications"}),
            media_type="application/json",
            status_code=200,
        )

    db_engine = await get_db_engine(request)
    if not db_engine:
        raise HTTPException(status_code=500, detail="Database engine not initialized")

    settings = get_settings()
    published_ids = []

    for note in notifications:
        ms_tenant_id = note.get("tenantId")
        resource = note.get("resource")
        client_state = note.get("clientState")

        if not ms_tenant_id or not resource:
            logger.warning("invalid_notification_value", notification=note)
            continue

        installation = await _installation(db_engine, ms_tenant_id)

        if not installation:
            logger.warning("msteams_meeting_installation_not_found", ms_tenant_id=ms_tenant_id)
            continue

        resolved_tenant_id = installation["tenant_id"]
        if not _client_state_ok(installation, client_state):
            logger.warning("invalid_msteams_client_state", ms_tenant_id=ms_tenant_id)
            continue

        await rate_limit.webhook(request, connection_key("teams-meetings", ms_tenant_id))

        # Get access token using Microsoft Graph credentials flow
        try:
            token = await get_msteams_access_token(
                ms_tenant_id=ms_tenant_id,
                client_id=settings.msteams_client_id,
                client_secret=settings.msteams_client_secret,
            )
        except Exception as e:
            logger.error("failed_to_get_msteams_token", ms_tenant_id=ms_tenant_id, error=str(e))
            continue

        # Parse meeting details to get topic and start time
        resource = _KEY_SEGMENT.sub(r"/\1", str(resource))
        meeting_match = re.search(r"users/([^/]+)/onlineMeetings/([^/]+)", resource)
        topic = "Teams Meeting"
        meeting_start = None
        host_email = "unknown@msteams.local"

        if meeting_match:
            user_id = meeting_match.group(1)
            meeting_id = meeting_match.group(2)
            meeting_url = f"{settings.graph_api}/users/{user_id}/onlineMeetings/{meeting_id}"

            try:
                meet_details = await fetch_graph_api(meeting_url, token)
                topic = meet_details.get("subject", "Teams Meeting")
                start_time_str = meet_details.get("startDateTime")

                # Fetch host info
                participants = meet_details.get("participants", {})
                organizer = participants.get("organizer", {})
                identity = organizer.get("identity", {})
                user_identity = identity.get("user", {})
                host_email = (
                    organizer.get("upn") or user_identity.get("id") or "unknown@msteams.local"
                )

                if start_time_str:
                    # Clean up date format
                    if start_time_str.endswith("Z"):
                        start_time_str = start_time_str[:-1] + "+00:00"
                    from datetime import UTC, datetime

                    meeting_start = datetime.fromisoformat(start_time_str)
                    if meeting_start.tzinfo is None:
                        meeting_start = meeting_start.replace(tzinfo=UTC)
            except Exception as e:
                logger.warning(
                    "failed_to_fetch_meeting_details", meeting_id=meeting_id, error=str(e)
                )

        if not meeting_start:
            from datetime import UTC, datetime

            meeting_start = datetime.now(UTC)

        # Retrieve transcript WebVTT content
        transcript_url = f"{settings.graph_api}/{resource}/content?$format=text/vtt"
        try:
            vtt_content = await fetch_graph_transcript_content(transcript_url, token)
        except Exception as e:
            logger.error("failed_to_fetch_msteams_vtt_content", resource=resource, error=str(e))
            continue

        # Parse and chunk transcript
        events = await parse_and_chunk_transcript(
            vtt_text=vtt_content,
            db_engine=db_engine,
            tenant_id=resolved_tenant_id,
            meeting_id=resource,  # Unique resource path represents meeting ID
            meeting_topic=topic,
            meeting_start=meeting_start,
            host_email_or_name=host_email,
            platform=SourcePlatform.MS_TEAMS,
            connection=connection_key("teams-meetings", installation["ms_tenant_id"]),
        )

        if not events:
            logger.info("empty_msteams_transcript_ignored", resource=resource)
            continue

        # Publish events to Redis
        ctx = getattr(request.app.state, "ctx", None)
        if not ctx or not ctx.redis_client:
            raise HTTPException(status_code=500, detail="Redis client not initialized")

        bus = RedisStreamsEventBus(ctx.redis_client)
        for event in events:
            msg_id = await bus.publish(topic="events.raw.meetings", event=event)
            published_ids.append(str(event.event_id))
            logger.info(
                "msteams_transcript_chunk_published", event_id=str(event.event_id), msg_id=msg_id
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
