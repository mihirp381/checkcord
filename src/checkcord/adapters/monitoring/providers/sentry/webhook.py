import hashlib
import hmac
import json
from uuid import UUID

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.adapters.monitoring.providers.sentry.parser import parse_sentry_webhook_event
from checkcord.adapters.monitoring.providers.sentry.schemas import SentryWebhookPayload
from checkcord.api import rate_limit
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core.config import get_settings
from checkcord.core.crypto import decrypt_secret
from checkcord.services.connections import connection_key
from checkcord.storage.models import SentryInstallation

logger = structlog.get_logger(__name__).bind(component="sentry_webhook")

router = APIRouter()


async def get_db_engine(request: Request) -> AsyncEngine:
    ctx = getattr(request.app.state, "ctx", None)
    if ctx and hasattr(ctx, "db_engine") and ctx.db_engine:
        from typing import cast

        return cast(AsyncEngine, ctx.db_engine)
    engine = getattr(request.app.state, "db_engine", None)
    if not engine:
        settings = get_settings()
        engine = create_async_engine(settings.database_url)
        request.app.state.db_engine = engine
    return engine


@router.post("/api/v1/webhooks/sentry/{installation_id}", status_code=202)
async def sentry_webhook(
    request: Request,
    installation_id: UUID,
    sentry_hook_signature: str = Header(...),
) -> Response:
    body_bytes = await request.body()
    try:
        body_json = json.loads(body_bytes)
    except Exception as e:
        logger.error("malformed_json_payload", error=str(e))
        raise HTTPException(status_code=400, detail="Malformed JSON payload") from e

    # Resolve installation globally (bypassing RLS since tenant
    # is resolved from the URL path variable)
    db_engine = await get_db_engine(request)
    async with db_engine.connect() as conn:
        from sqlalchemy import text

        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        stmt = select(SentryInstallation).where(SentryInstallation.id == installation_id)
        res = await conn.execute(stmt)
        installation = res.mappings().fetchone()

    if not installation:
        logger.warning("sentry_installation_not_found", installation_id=str(installation_id))
        raise HTTPException(status_code=401, detail="Unauthorized: Installation not found")

    tenant_id: UUID = installation["tenant_id"]
    webhook_secret = decrypt_secret(installation["webhook_secret"])

    # Validate Sentry signature
    expected = hmac.new(webhook_secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sentry_hook_signature):
        logger.warning("invalid_sentry_signature", installation_id=str(installation_id))
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid signature")

    await rate_limit.webhook(request, connection_key("sentry", installation_id))

    # Parse and normalize
    try:
        payload = SentryWebhookPayload.model_validate(body_json)
        event = parse_sentry_webhook_event(payload, tenant_id).model_copy(
            update={"connection": connection_key("sentry", installation_id)}
        )
    except Exception as parse_err:
        logger.exception("failed_to_parse_sentry_event", error=str(parse_err))
        return Response(
            content=json.dumps({"status": "error_parsed", "details": str(parse_err)}),
            media_type="application/json",
            status_code=202,
        )

    # Initialize EventBus & Publish to events.raw.monitoring stream
    ctx = getattr(request.app.state, "ctx", None)
    if not ctx or not ctx.redis_client:
        raise HTTPException(status_code=500, detail="Redis client not initialized")
    bus = RedisStreamsEventBus(ctx.redis_client)

    try:
        msg_id = await bus.publish(topic="events.raw.monitoring", event=event)
        logger.info("sentry_event_published", event_id=str(event.event_id), msg_id=msg_id)
        return Response(
            content=json.dumps({"status": "processed", "event_id": str(event.event_id)}),
            media_type="application/json",
            status_code=202,
        )
    except Exception as pub_err:
        logger.exception(
            "failed_to_publish_sentry_event", event_id=str(event.event_id), error=str(pub_err)
        )
        raise HTTPException(status_code=500, detail="Failed to publish to stream") from pub_err
