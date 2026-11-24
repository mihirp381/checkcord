import hashlib
import hmac
import json
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.adapters.tasks.providers.jira.parser import parse_jira_webhook_event
from checkcord.adapters.tasks.providers.jira.schemas import JiraWebhookPayload
from checkcord.api import rate_limit
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core.config import get_settings
from checkcord.core.crypto import decrypt_secret
from checkcord.services import scope
from checkcord.services.connections import connection_key
from checkcord.storage.models import JiraInstallation

logger = structlog.get_logger(__name__).bind(component="jira_webhook")

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


def extract_jira_host_from_payload(payload_json: dict[str, Any]) -> str | None:
    """Extract the Jira host (netloc) from self URLs in the payload."""
    from urllib.parse import urlparse

    # Try issue.self
    issue_self = payload_json.get("issue", {}).get("self")
    if issue_self and isinstance(issue_self, str):
        return str(urlparse(issue_self).netloc)

    # Try comment.self
    comment_self = payload_json.get("comment", {}).get("self")
    if comment_self and isinstance(comment_self, str):
        return str(urlparse(comment_self).netloc)

    # Try user.self
    user_self = payload_json.get("user", {}).get("self")
    if user_self and isinstance(user_self, str):
        return str(urlparse(user_self).netloc)

    return None


DELIVERY_SECONDS = 7 * 24 * 3600


@router.post("/api/v1/webhooks/jira", status_code=202)
async def jira_webhook(request: Request) -> Response:
    """FastAPI endpoint to receive and process Jira webhooks."""
    body_bytes = await request.body()
    try:
        body_json = json.loads(body_bytes)
    except Exception as e:
        logger.error("malformed_json_payload", error=str(e))
        raise HTTPException(status_code=400, detail="Malformed JSON payload") from e

    jira_host = extract_jira_host_from_payload(body_json)
    if not jira_host:
        # Check if it is a ping or validation event
        if body_json.get("webhookEvent") == "webhook_test" or "webhookEvent" not in body_json:
            return Response(
                content=json.dumps({"status": "ping_ok"}),
                media_type="application/json",
                status_code=200,
            )
        logger.warning("missing_jira_host_info")
        raise HTTPException(status_code=400, detail="Missing Jira host information")

    # Lookup JiraInstallation globally (bypassing RLS)
    db_engine = await get_db_engine(request)
    async with db_engine.connect() as conn:
        from sqlalchemy import text

        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        stmt = select(JiraInstallation).where(
            JiraInstallation.jira_host.in_([jira_host, f"http://{jira_host}"])
        )
        res = await conn.execute(stmt)
        installation = res.mappings().fetchone()

    if not installation:
        logger.warning("installation_not_found", jira_host=jira_host)
        raise HTTPException(status_code=401, detail="Unauthorized: Jira host not installed")

    tenant_id: UUID = installation["tenant_id"]
    jira_host = installation["jira_host"]
    webhook_secret = decrypt_secret(installation["webhook_secret"])

    # Validate signature from X-Hub-Signature or X-Hub-Signature-256
    sig_header = request.headers.get("x-hub-signature-256") or request.headers.get(
        "x-hub-signature"
    )
    if not sig_header:
        logger.warning("missing_signature_header")
        raise HTTPException(status_code=401, detail="Unauthorized: Missing signature header")

    if sig_header.startswith("sha256="):
        sig_hex = sig_header[7:]
    else:
        sig_hex = sig_header

    expected = hmac.new(webhook_secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, sig_hex):
        logger.warning("invalid_webhook_signature", jira_host=jira_host)
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid signature")

    await rate_limit.webhook(request, connection_key("jira", jira_host))

    if body_json.get("webhookEvent") == "webhook_test":
        return Response(
            content=json.dumps({"status": "ok"}), media_type="application/json", status_code=200
        )

    issue = body_json.get("issue") or {}
    project = ((issue.get("fields") or {}).get("project") or {}).get("key")
    if not await scope.allows(
        db_engine,
        tenant_id,
        connection_key("jira", jira_host),
        project or str(issue.get("key") or "").partition("-")[0],
    ):
        return Response(
            content=json.dumps({"status": "out_of_scope"}),
            media_type="application/json",
            status_code=200,
        )

    try:
        payload = JiraWebhookPayload.model_validate(body_json)
    except Exception as parse_err:
        logger.warning("failed_to_parse_jira_payload", error=str(parse_err))
        raise HTTPException(
            status_code=400, detail=f"Invalid payload schema: {str(parse_err)}"
        ) from parse_err

    try:
        event = await parse_jira_webhook_event(payload, db_engine, tenant_id)
        container = project or str(issue.get("key") or "").partition("-")[0]
        event = event.model_copy(
            update={
                "connection": connection_key("jira", installation["jira_host"]),
                "audience_container": scope.normalize(container) if container else None,
            }
        )
    except Exception as process_err:
        logger.exception("failed_to_process_jira_event", error=str(process_err))
        raise HTTPException(
            status_code=500, detail=f"Internal processing error: {str(process_err)}"
        ) from process_err

    if not event:
        logger.info("ignored_jira_event", event_type=payload.webhookEvent)
        return Response(
            content=json.dumps({"status": "ignored"}),
            media_type="application/json",
            status_code=200,
        )

    ctx = getattr(request.app.state, "ctx", None)
    if not ctx or not ctx.redis_client:
        raise HTTPException(status_code=500, detail="Redis client not initialized")
    bus = RedisStreamsEventBus(ctx.redis_client)
    delivery = request.headers.get("x-atlassian-webhook-identifier")
    seen = f"jira:delivery:{tenant_id}:{delivery}" if delivery else None
    if seen and await ctx.redis_client.exists(seen):
        logger.info("jira_redelivery_ignored", delivery=delivery)
        return Response(
            content=json.dumps({"status": "duplicate"}),
            media_type="application/json",
            status_code=200,
        )

    try:
        # events.raw.chat takes RawChatPayload only; Jira ticket events are
        # CanonicalEvents and go to events.raw.tasks with document ingestion.
        msg_id = await bus.publish(topic="events.raw.tasks", event=event)
        if seen:
            await ctx.redis_client.set(seen, "1", ex=DELIVERY_SECONDS)
        logger.info("jira_event_published", event_id=str(event.event_id), msg_id=msg_id)
        return Response(
            content=json.dumps({"status": "processed", "event_ids": [str(event.event_id)]}),
            media_type="application/json",
            status_code=202,
        )
    except Exception as pub_err:
        logger.exception(
            "failed_to_publish_jira_event", event_id=str(event.event_id), error=str(pub_err)
        )
        raise HTTPException(status_code=500, detail="Failed to publish to message bus") from pub_err
