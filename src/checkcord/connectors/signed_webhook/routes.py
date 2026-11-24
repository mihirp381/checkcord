import hashlib
import hmac
import time
from uuid import UUID

import structlog
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.api import rate_limit
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.connectors import installations
from checkcord.connectors.signed_webhook.payload import SignedWebhookEvent, to_event
from checkcord.services import people, scope
from checkcord.services.connections import connection_key
from checkcord.storage.postgres import tenant_context

logger = structlog.get_logger(__name__).bind(component="signed_webhook")

router = APIRouter()

NAME = "webhook"
TOLERANCE_SECONDS = 300
TOPIC = "events.raw.custom"


def signature(secret: str, timestamp: str, body: bytes) -> str:
    """``sha256=`` and the hex HMAC-SHA256 of ``timestamp.body`` under the connection secret."""
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def _fresh(timestamp: str | None, now: float) -> bool:
    try:
        return timestamp is not None and abs(now - int(timestamp)) <= TOLERANCE_SECONDS
    except ValueError:
        return False


class Accepted(BaseModel):
    status: str
    event_id: UUID | None = None
    unknown_emails: int = 0


async def _readers(
    engine: AsyncEngine, tenant_id: UUID, item: SignedWebhookEvent
) -> tuple[list[UUID], int]:
    """The people the audience names, and how many of its emails match nobody."""
    found = set(item.audience.identity_ids)
    missing = 0
    if item.audience.emails and not item.audience.public:
        async with tenant_context(engine, tenant_id) as conn:
            for email in item.audience.emails:
                rows = await people.by_email(conn, tenant_id, email)
                active = [r.id for r in rows if r.active]
                found.update(active)
                missing += not active
    return sorted(found, key=str), missing


@router.post(
    "/api/v1/webhooks/signed/{installation_id}",
    status_code=202,
    summary="Receive a signed webhook event",
)
async def signed_webhook(
    request: Request,
    installation_id: UUID,
    x_checkcord_timestamp: str | None = Header(None),
    x_checkcord_signature: str | None = Header(None),
) -> Accepted:
    body = await request.body()
    ctx = request.app.state.ctx
    found = await installations.by_id(ctx.db_engine, NAME, installation_id)
    if (
        found is None
        or not found.secret
        or not _fresh(x_checkcord_timestamp, time.time())
        or not x_checkcord_signature
        or not hmac.compare_digest(
            x_checkcord_signature.encode(),
            signature(found.secret, x_checkcord_timestamp or "", body).encode(),
        )
    ):
        logger.warning("signed_webhook.rejected", installation_id=str(installation_id))
        raise HTTPException(status_code=401, detail="Invalid signature")

    key = connection_key(NAME, found.identifier)
    await rate_limit.webhook(request, key)
    try:
        item = SignedWebhookEvent.model_validate_json(body)
    except ValidationError as err:
        raise HTTPException(
            status_code=422, detail=err.errors(include_url=False, include_context=False)
        ) from err

    if not await scope.allows(ctx.db_engine, found.tenant_id, key, item.container):
        return Accepted(status="skipped")
    readers, unknown = await _readers(ctx.db_engine, found.tenant_id, item)
    event = to_event(item, found.tenant_id, key, readers)
    if not ctx.redis_client:
        raise HTTPException(status_code=500, detail="Redis client not initialized")
    await RedisStreamsEventBus(ctx.redis_client).publish(TOPIC, event)
    return Accepted(status="accepted", event_id=event.event_id, unknown_emails=unknown)
