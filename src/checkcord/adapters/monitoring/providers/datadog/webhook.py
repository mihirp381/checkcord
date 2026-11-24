import hmac
import json
from uuid import UUID

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.adapters.monitoring.providers.datadog.parser import parse_datadog_webhook_event
from checkcord.adapters.monitoring.providers.datadog.schemas import DatadogWebhookPayload
from checkcord.api import rate_limit
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core.config import get_settings
from checkcord.core.crypto import decrypt_secret
from checkcord.services.connections import connection_key
from checkcord.storage.models import DatadogInstallation

logger = structlog.get_logger(__name__).bind(component="datadog_webhook")

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


@router.post("/api/v1/webhooks/datadog/{installation_id}", status_code=202)
async def datadog_webhook(
    request: Request,
    installation_id: UUID,
    x_datadog_token: str | None = Header(None),
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
        stmt = select(DatadogInstallation).where(DatadogInstallation.id == installation_id)
        res = await conn.execute(stmt)
        installation = res.mappings().fetchone()

    if not installation:
        logger.warning("datadog_installation_not_found", installation_id=str(installation_id))
        raise HTTPException(status_code=401, detail="Unauthorized: Installation not found")

    tenant_id: UUID = installation["tenant_id"]
    webhook_secret = decrypt_secret(installation["webhook_secret"])

    provided_token = x_datadog_token
    if not provided_token or not hmac.compare_digest(
        provided_token.encode(), webhook_secret.encode()
    ):
        logger.warning("invalid_datadog_token", installation_id=str(installation_id))
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid token")

    await rate_limit.webhook(request, connection_key("datadog", installation_id))

    # Parse and normalize
    try:
        payload = DatadogWebhookPayload.model_validate(body_json)
        event = parse_datadog_webhook_event(payload, tenant_id).model_copy(
            update={"connection": connection_key("datadog", installation_id)}
        )
    except Exception as parse_err:
        logger.exception("failed_to_parse_datadog_event", error=str(parse_err))
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
        logger.info("datadog_event_published", event_id=str(event.event_id), msg_id=msg_id)
        return Response(
            content=json.dumps({"status": "processed", "event_id": str(event.event_id)}),
            media_type="application/json",
            status_code=202,
        )
    except Exception as pub_err:
        logger.exception(
            "failed_to_publish_datadog_event", event_id=str(event.event_id), error=str(pub_err)
        )
        raise HTTPException(status_code=500, detail="Failed to publish to stream") from pub_err
