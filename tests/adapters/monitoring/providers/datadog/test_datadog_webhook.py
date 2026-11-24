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

WEBHOOK_SECRET = "datadog_secret_token"


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
async def datadog_installation() -> AsyncGenerator[dict[str, Any], None]:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    tenant_id = uuid4()
    inst_id = uuid4()

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text(
                "INSERT INTO datadog_installations (id, tenant_id, webhook_secret) "
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
            text("DELETE FROM datadog_installations WHERE id = :id"),
            {"id": inst_id},
        )
    await engine.dispose()


async def test_datadog_webhook_success(datadog_installation: dict[str, Any]) -> None:
    settings = get_settings()
    payload = {
        "id": "datadog-alert-1",
        "title": "[service: billing-service] database connection failure on backend-db",
        "body": (
            "Database connection failed for billing-service. "
            "Triggered by high load. Repos affected: test_org/test_repo"
        ),
        "event_type": "incident",
        "url": "https://app.datadoghq.com/incidents/1",
        "date": 1780000000,
    }
    payload_bytes = json.dumps(payload).encode()
    headers = {"X-Datadog-Token": WEBHOOK_SECRET}

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/webhooks/datadog/{datadog_installation['id']}",
            content=payload_bytes,
            headers=headers,
        )

    assert response.status_code == 202
    assert response.json()["status"] == "processed"

    events = await _drain_redis_stream(settings.redis_url)
    assert len(events) == 1
    event = events[0]
    assert event["source_platform"] == "datadog"
    assert event["source_type"] == "incident"
    assert event["event_action"] == "created"
    assert event["source_id"] == "datadog:event:datadog-alert-1"
    assert "database connection failure" in event["content_raw"]

    # Assert entities
    entities = event["entities"]
    assert len(entities) == 2  # service + repo
    assert any(
        ent["entity_type"] == "service" and ent["entity_id"] == "billing-service"
        for ent in entities
    )
    assert any(
        ent["entity_type"] == "repo" and ent["entity_id"] == "test_org/test_repo"
        for ent in entities
    )

    # Assert incident metadata
    assert event["incident_metadata"] is not None
    assert event["incident_metadata"]["severity"] == "error"
    assert event["incident_metadata"]["environment"] == "unknown"
    assert "billing-service" in event["incident_metadata"]["affected_services"]


async def test_datadog_webhook_ignores_a_query_token(datadog_installation: dict[str, Any]) -> None:
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/webhooks/datadog/{datadog_installation['id']}?token={WEBHOOK_SECRET}",
            content=json.dumps({"title": "high error rate"}).encode(),
        )

    assert response.status_code == 401


async def test_datadog_webhook_invalid_token(datadog_installation: dict[str, Any]) -> None:
    payload = {"title": "some alert", "body": "details"}
    payload_bytes = json.dumps(payload).encode()
    headers = {"X-Datadog-Token": "badtoken"}

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/webhooks/datadog/{datadog_installation['id']}",
            content=payload_bytes,
            headers=headers,
        )
    assert response.status_code == 401
