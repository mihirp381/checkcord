from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.adapters.meetings.providers.msteams.webhook import router as msteams_router
from checkcord.core.config import get_settings

pytestmark = pytest.mark.integration


class MockAppCtx:
    def __init__(self, db_engine: AsyncEngine, redis_client: Redis) -> None:
        self.db_engine = db_engine
        self.redis_client = redis_client
        self.settings = get_settings()


@pytest.fixture
def app_with_teams(async_redis_client: Redis) -> FastAPI:
    app = FastAPI()
    app.include_router(msteams_router)

    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    app.state.ctx = MockAppCtx(db_engine=engine, redis_client=async_redis_client)
    return app


@pytest.mark.asyncio
async def test_msteams_webhook_validation(app_with_teams: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app_with_teams), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/webhooks/msteams/meetings?validationToken=test_validation_token_123"
        )

    assert resp.status_code == 200
    assert resp.text == "test_validation_token_123"


@pytest.mark.asyncio
async def test_msteams_webhook_notification(app_with_teams: FastAPI) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    tenant_id = uuid4()
    ms_tenant_id = f"ms-{uuid4().hex[:6]}"
    webhook_secret = "msteams-secret-state"

    # Seed installation
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text(
                "INSERT INTO msteams_meeting_installations "
                "(id, tenant_id, ms_tenant_id, webhook_secret) "
                "VALUES (:id, :tenant_id, :ms_tenant_id, :webhook_secret)"
            ),
            {
                "id": uuid4(),
                "tenant_id": tenant_id,
                "ms_tenant_id": ms_tenant_id,
                "webhook_secret": webhook_secret,
            },
        )

    vtt_mock = """WEBVTT

1
00:00:01.000 --> 00:00:03.000
<v Organizer Name>Meeting started.
"""

    payload = {
        "value": [
            {
                "subscriptionId": "sub-123",
                "clientState": webhook_secret,
                "resource": (
                    "users/organizer@example.com/onlineMeetings/meet-abc/transcripts/trans-xyz"
                ),
                "tenantId": ms_tenant_id,
                "changeType": "updated",
            }
        ]
    }

    try:
        # Patch dependencies: get_msteams_access_token, fetch_graph_api,
        # fetch_graph_transcript_content
        with (
            patch(
                "checkcord.adapters.meetings.providers.msteams.webhook.get_msteams_access_token",
                new_callable=AsyncMock,
                return_value="mock_graph_token",
            ) as mock_token,
            patch(
                "checkcord.adapters.meetings.providers.msteams.webhook.fetch_graph_api",
                new_callable=AsyncMock,
                return_value={
                    "subject": "Architecture Discussion",
                    "startDateTime": "2026-06-25T04:00:00Z",
                    "participants": {
                        "organizer": {
                            "identity": {
                                "user": {
                                    "id": "organizer@example.com",
                                    "displayName": "Organizer Name",
                                }
                            }
                        }
                    },
                },
            ) as mock_details,
            patch(
                "checkcord.adapters.meetings.providers.msteams.webhook.fetch_graph_transcript_content",
                new_callable=AsyncMock,
                return_value=vtt_mock,
            ) as mock_vtt,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_with_teams), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/webhooks/msteams/meetings", json=payload)

            assert resp.status_code == 202
            assert mock_token.call_count == 1
            assert mock_details.call_count == 1
            assert mock_vtt.call_count == 1

            # Verify events in Redis stream
            redis = app_with_teams.state.ctx.redis_client
            events_in_stream = await redis.xrange("events.raw.meetings", min="-", max="+")
            assert len(events_in_stream) == 1

            # Verify reconstructed event
            redis_dict = {
                k.decode("utf-8"): v.decode("utf-8") for k, v in events_in_stream[0][1].items()
            }
            from checkcord.models.event import CanonicalEvent

            event = CanonicalEvent.from_redis_dict(redis_dict)
            assert event.source_platform == "ms_teams"
            assert event.source_type == "meeting_transcript"
            assert "[Chunk 1 of 1]" in event.content_raw
            assert "Organizer Name: Meeting started." in event.content_clean

    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM msteams_meeting_installations WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
