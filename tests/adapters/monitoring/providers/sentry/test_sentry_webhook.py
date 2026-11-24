import hashlib
import hmac
import json
from collections.abc import AsyncGenerator
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.core.config import get_settings
from checkcord.main import app
from checkcord.models.event import CanonicalEvent

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

WEBHOOK_SECRET = "sentry_secret"


async def _drain_redis_stream(
    redis_url: str, stream_name: str = "events.raw.monitoring"
) -> list[dict[str, Any]]:
    client = Redis.from_url(redis_url)
    try:
        raw_streams: Any = await client.xread(streams={stream_name: "0-0"}, count=100)
        events: list[dict[str, Any]] = []
        if raw_streams:
            for _, messages in raw_streams:
                for _msg_id, fields in messages:
                    decoded = {
                        (k.decode("utf-8") if isinstance(k, bytes) else k): (
                            v.decode("utf-8") if isinstance(v, bytes) else v
                        )
                        for k, v in fields.items()
                    }
                    event = CanonicalEvent.from_redis_dict(decoded)
                    events.append(event.model_dump(mode="json"))
        return events
    finally:
        await client.aclose()


async def _flush_redis_stream(redis_url: str, stream_name: str = "events.raw.monitoring") -> None:
    client = Redis.from_url(redis_url)
    try:
        await client.delete(stream_name)
    except Exception:
        pass


@pytest_asyncio.fixture
async def sentry_installation() -> AsyncGenerator[dict[str, Any], None]:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    tenant_id = uuid4()
    inst_id = uuid4()

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text(
                "INSERT INTO sentry_installations (id, tenant_id, webhook_secret) "
                "VALUES (:id, :tenant_id, :secret)"
            ),
            {"id": inst_id, "tenant_id": tenant_id, "secret": WEBHOOK_SECRET},
        )

    yield {"tenant_id": tenant_id, "id": inst_id}

    # Teardown
    await _flush_redis_stream(settings.redis_url)
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text("DELETE FROM sentry_installations WHERE id = :id"),
            {"id": inst_id},
        )
    await engine.dispose()


async def test_sentry_webhook_success(sentry_installation: dict[str, Any]) -> None:
    settings = get_settings()
    payload = {
        "id": "sentry-alert-1",
        "project": "my-backend",
        "project_name": "My Backend Service",
        "project_slug": "my-backend",
        "level": "error",
        "message": "ValueError: Invalid configuration",
        "url": "https://sentry.io/org/proj/issues/123",
        "event": {
            "event_id": "abc123event",
            "title": "ValueError: Invalid configuration",
            "culprit": "src/checkcord/core/config.py in get_settings",
            "timestamp": 1780000000.0,
            "exception": {
                "values": [
                    {
                        "type": "ValueError",
                        "value": "Invalid configuration",
                        "stacktrace": {
                            "frames": [
                                {
                                    "filename": "src/checkcord/main.py",
                                    "function": "create_app",
                                    "lineno": 200,
                                    "in_app": True,
                                },
                                {
                                    "filename": "src/checkcord/core/config.py",
                                    "function": "get_settings",
                                    "lineno": 75,
                                    "in_app": True,
                                },
                            ]
                        },
                    }
                ]
            },
        },
    }
    payload_bytes = json.dumps(payload).encode()
    signature = hmac.new(WEBHOOK_SECRET.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    headers = {"Sentry-Hook-Signature": signature}

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/webhooks/sentry/{sentry_installation['id']}",
            content=payload_bytes,
            headers=headers,
        )

    assert response.status_code == 202
    assert response.json()["status"] == "processed"

    events = await _drain_redis_stream(settings.redis_url)
    assert len(events) == 1
    event = events[0]
    assert event["source_platform"] == "sentry"
    assert event["source_type"] == "incident"
    assert event["event_action"] == "created"
    assert event["source_id"] == "sentry:event:abc123event"
    assert "ValueError: Invalid configuration" in event["content_raw"]
    assert "src/checkcord/core/config.py" in event["content_raw"]

    # Assert extracted entities
    entities = event["entities"]
    assert len(entities) == 3  # service + 2 files
    # Check project service entity
    assert any(
        ent["entity_type"] == "service" and ent["entity_id"] == "my-backend" for ent in entities
    )
    # Check file entities
    assert any(
        ent["entity_type"] == "file" and ent["entity_id"] == "src/checkcord/main.py"
        for ent in entities
    )
    assert any(
        ent["entity_type"] == "file" and ent["entity_id"] == "src/checkcord/core/config.py"
        for ent in entities
    )

    # Assert incident metadata
    assert event["incident_metadata"] is not None
    assert event["incident_metadata"]["severity"] == "error"
    assert event["incident_metadata"]["environment"] == "unknown"
    assert "my-backend" in event["incident_metadata"]["affected_services"]


async def test_sentry_webhook_invalid_signature(sentry_installation: dict[str, Any]) -> None:
    payload = {"message": "unauthorized alert"}
    payload_bytes = json.dumps(payload).encode()
    headers = {"Sentry-Hook-Signature": "badsig"}

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/webhooks/sentry/{sentry_installation['id']}",
            content=payload_bytes,
            headers=headers,
        )
    assert response.status_code == 401
