import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.adapters.meetings.providers.zoom.webhook import router as zoom_router
from checkcord.core.config import get_settings

pytestmark = pytest.mark.integration


class MockAppCtx:
    def __init__(self, db_engine: AsyncEngine, redis_client: Redis) -> None:
        self.db_engine = db_engine
        self.redis_client = redis_client
        self.settings = get_settings()


@pytest.fixture
def app_with_zoom(async_redis_client: Redis) -> FastAPI:
    app = FastAPI()
    app.include_router(zoom_router)

    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    app.state.ctx = MockAppCtx(db_engine=engine, redis_client=async_redis_client)
    return app


@pytest.mark.asyncio
async def test_zoom_webhook_url_validation(app_with_zoom: FastAPI) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    tenant_id = uuid4()
    zoom_account_id = f"acc_{uuid4().hex[:6]}"
    webhook_secret = "secret-token-zoom"

    # Seed installation
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text(
                "INSERT INTO zoom_installations (id, tenant_id, zoom_account_id, webhook_secret) "
                "VALUES (:id, :tenant_id, :zoom_account_id, :webhook_secret)"
            ),
            {
                "id": uuid4(),
                "tenant_id": tenant_id,
                "zoom_account_id": zoom_account_id,
                "webhook_secret": webhook_secret,
            },
        )

    try:
        # Prepare URL Validation payload
        payload = {
            "event": "endpoint.url_validation",
            "event_ts": 123456789,
            "payload": {"plainToken": "validation_token_123"},
        }

        body_str = json.dumps(payload)
        timestamp = str(int(time.time()))

        # Calculate signature
        message = f"v0:{timestamp}:{body_str}"
        sig = (
            "v0=" + hmac.new(webhook_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        )

        headers = {
            "x-zm-request-timestamp": timestamp,
            "x-zm-signature": sig,
            "Content-Type": "application/json",
        }

        # Call the endpoint using httpx AsyncClient
        async with AsyncClient(
            transport=ASGITransport(app=app_with_zoom), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/webhooks/zoom?zoom_account_id={zoom_account_id}",
                content=body_str,
                headers=headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["plainToken"] == "validation_token_123"
        assert "encryptedToken" in data

        # Verify invalid signature returns 401
        headers["x-zm-signature"] = "v0=badsignature"
        async with AsyncClient(
            transport=ASGITransport(app=app_with_zoom), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/webhooks/zoom?zoom_account_id={zoom_account_id}",
                content=body_str,
                headers=headers,
            )
        assert resp.status_code == 401

    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM zoom_installations WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )


@pytest.mark.asyncio
async def test_zoom_webhook_recording_completed(app_with_zoom: FastAPI) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    tenant_id = uuid4()
    zoom_account_id = f"acc_{uuid4().hex[:6]}"
    webhook_secret = "secret-token-zoom"

    # Seed installation
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text(
                "INSERT INTO zoom_installations (id, tenant_id, zoom_account_id, webhook_secret) "
                "VALUES (:id, :tenant_id, :zoom_account_id, :webhook_secret)"
            ),
            {
                "id": uuid4(),
                "tenant_id": tenant_id,
                "zoom_account_id": zoom_account_id,
                "webhook_secret": webhook_secret,
            },
        )

    vtt_mock = """WEBVTT

1
00:00:01.000 --> 00:00:03.000
<v alice@example.com>Hello world.
"""

    payload = {
        "event": "recording.completed",
        "event_ts": 123456789,
        "payload": {
            "account_id": zoom_account_id,
            "object": {
                "uuid": "meeting-uuid-1",
                "id": 987654321,
                "host_id": "host-id-1",
                "host_email": "alice@example.com",
                "topic": "Weekly Standup",
                "start_time": "2026-06-25T04:00:00Z",
                "duration": 30,
                "recording_files": [
                    {
                        "id": "file-id-1",
                        "meeting_id": "meeting-uuid-1",
                        "file_type": "TRANSCRIPT",
                        "download_url": "https://zoom.us/recording/download/vtt-file",
                        "status": "completed",
                    }
                ],
            },
        },
        "download_token": "tok-" + uuid4().hex,
    }

    body_str = json.dumps(payload)
    timestamp = str(int(time.time()))
    message = f"v0:{timestamp}:{body_str}"
    sig = "v0=" + hmac.new(webhook_secret.encode(), message.encode(), hashlib.sha256).hexdigest()

    headers = {
        "x-zm-request-timestamp": timestamp,
        "x-zm-signature": sig,
        "Content-Type": "application/json",
    }

    try:
        # Patch download_transcript to return mock VTT content
        with patch(
            "checkcord.adapters.meetings.providers.zoom.webhook.download_transcript",
            new_callable=AsyncMock,
            return_value=vtt_mock,
        ) as mock_download:
            async with AsyncClient(
                transport=ASGITransport(app=app_with_zoom), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/webhooks/zoom", content=body_str, headers=headers)

            assert resp.status_code == 202
            assert mock_download.call_count == 1
            assert mock_download.call_args[0][0] == "https://zoom.us/recording/download/vtt-file"
            assert mock_download.call_args[0][1] == payload["download_token"]

            # Verify events are in Redis stream
            redis = app_with_zoom.state.ctx.redis_client
            events_in_stream = await redis.xrange("events.raw.meetings", min="-", max="+")
            assert len(events_in_stream) == 1

            # Verify parsed content
            redis_dict = {
                k.decode("utf-8"): v.decode("utf-8") for k, v in events_in_stream[0][1].items()
            }
            from checkcord.models.event import CanonicalEvent

            event = CanonicalEvent.from_redis_dict(redis_dict)
            assert event.source_platform == "zoom"
            assert event.source_type == "meeting_transcript"
            assert "[Chunk 1 of 1]" in event.content_raw
            assert "alice@example.com: Hello world." in event.content_clean

    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM zoom_installations WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [360, 3600, -360])
async def test_zoom_webhook_rejects_stale_timestamp(app_with_zoom: FastAPI, age: int) -> None:
    engine = create_async_engine(get_settings().database_url)
    tenant_id = uuid4()
    zoom_account_id = f"acc_{uuid4().hex[:6]}"
    webhook_secret = "secret-token-zoom"
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text(
                "INSERT INTO zoom_installations (id, tenant_id, zoom_account_id, webhook_secret) "
                "VALUES (:id, :tenant_id, :zoom_account_id, :webhook_secret)"
            ),
            {
                "id": uuid4(),
                "tenant_id": tenant_id,
                "zoom_account_id": zoom_account_id,
                "webhook_secret": webhook_secret,
            },
        )
    try:
        body_str = json.dumps(
            {"event": "endpoint.url_validation", "payload": {"plainToken": "tok"}}
        )
        timestamp = str(int(time.time()) - age)
        message = f"v0:{timestamp}:{body_str}"
        sig = (
            "v0=" + hmac.new(webhook_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        )
        async with AsyncClient(
            transport=ASGITransport(app=app_with_zoom), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/webhooks/zoom?zoom_account_id={zoom_account_id}",
                content=body_str,
                headers={
                    "x-zm-request-timestamp": timestamp,
                    "x-zm-signature": sig,
                    "Content-Type": "application/json",
                },
            )
        assert resp.status_code == 401
        assert "timestamp" in resp.json()["detail"].lower()
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM zoom_installations WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
        await engine.dispose()
