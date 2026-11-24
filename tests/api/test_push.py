from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from key_helpers import KeySpec, seeded_keys, tenant_keys
from pubsub_test_helpers import assert_topic_not_dlqd, real_subscription, wait_for_message_in_stream
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core.config import get_settings
from checkcord.main import app
from checkcord.models.common import AccessControl
from checkcord.models.enums import AccessVisibility, AclType
from checkcord.models.event import CanonicalEvent
from checkcord.pipeline.enricher import PipelineEnricher
from checkcord.storage.postgres import PostgresEventStore, tenant_context

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="module", autouse=True)
def setup_tokens() -> Iterator[dict[str, str]]:
    """Setup settings mapping bearer tokens to tenant UUIDs for push tests."""
    tenant_a = str(uuid4())
    tenant_b = str(uuid4())
    token_a = "token_push_a"
    token_b = "token_push_b"
    tokens = {
        token_a: tenant_a,
        token_b: tenant_b,
    }
    with tenant_keys(tokens):
        yield tokens


_CLEANUP_TOPICS = [
    "events.raw.chat",
    "events.raw.custom",
    "events.raw.custom.dlq",
    "events.enriched.chat",
]


@pytest_asyncio.fixture
async def cleanup_events(setup_tokens: dict[str, str]) -> AsyncGenerator[None, None]:
    """Clean up events database rows and Redis stream after test run."""
    settings = get_settings()
    # Clean Redis stream
    redis_test_client = Redis.from_url(settings.redis_url)
    try:
        await redis_test_client.delete(*_CLEANUP_TOPICS)
    except Exception:
        pass
    await redis_test_client.aclose()

    yield

    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        # Bypass RLS to perform cleaning
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        for tenant_id in setup_tokens.values():
            await conn.execute(
                text("DELETE FROM events WHERE tenant_id = :tid"),
                {"tid": UUID(tenant_id)},
            )
    await engine.dispose()

    redis_test_client = Redis.from_url(settings.redis_url)
    try:
        await redis_test_client.delete(*_CLEANUP_TOPICS)
    except Exception:
        pass
    await redis_test_client.aclose()


@pytest.mark.asyncio
async def test_authentication(setup_tokens: dict[str, str]) -> None:
    """Verify that unauthorized requests to push API endpoints return 401."""
    with TestClient(app) as client:
        # 1. Test Ingest endpoint without token
        res_ingest_no_auth = client.post("/api/v1/ingest/custom", json={"content_raw": "test"})
        assert res_ingest_no_auth.status_code == 401

        # 2. Test Ingest endpoint with bad token
        res_ingest_bad_auth = client.post(
            "/api/v1/ingest/custom",
            json={"content_raw": "test"},
            headers={"Authorization": "Bearer bad_token"},
        )
        assert res_ingest_bad_auth.status_code == 401

        # 3. Test ACL Override endpoint without token
        res_acl_no_auth = client.post(
            f"/api/v1/admin/events/{uuid4()}/acl-override", json={"admin_overrides": []}
        )
        assert res_acl_no_auth.status_code == 401


@pytest.mark.asyncio
async def test_custom_data_ingestion(
    setup_tokens: dict[str, str], cleanup_events: AsyncGenerator[None, None]
) -> None:
    """Verify POST /api/v1/ingest/custom creates event and publishes it to Redis stream."""
    token_a = "token_push_a"
    tenant_a = UUID(setup_tokens[token_a])

    headers = {"Authorization": f"Bearer {token_a}"}
    payload = {
        "content_raw": "This is raw test content",
        "metadata": {"custom_field": "custom_val"},
    }

    with TestClient(app) as client:
        res = client.post("/api/v1/ingest/custom", json=payload, headers=headers)
        assert res.status_code == 201
        res_data = res.json()
        assert "event_id" in res_data
        event_id = UUID(res_data["event_id"])

        # Fetch published event from Redis Stream
        settings = get_settings()
        redis_client = Redis.from_url(settings.redis_url)
        streams = await redis_client.xread(streams={"events.raw.custom": 0}, count=100)
        await redis_client.aclose()

        assert streams, "No messages found in Redis stream events.raw.custom"
        # Find the message corresponding to our event_id
        message_found = False
        from typing import cast

        typed_streams = cast(list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]], streams)
        for _, messages in typed_streams:
            for _, fields in messages:
                # Fields are binary strings
                decoded_fields = {k.decode("utf-8"): v.decode("utf-8") for k, v in fields.items()}
                if decoded_fields.get("event_id") == str(event_id):
                    message_found = True
                    event = CanonicalEvent.from_redis_dict(decoded_fields)
                    assert event.tenant_id == tenant_a
                    assert event.content_raw == "This is raw test content"
                    assert event.content_clean == "This is raw test content"  # defaulted
                    # Assert default access control mapping: private & type custom
                    assert event.access_control.visibility == AccessVisibility.PRIVATE
                    assert event.access_control.acl_type == AclType.CUSTOM
                    assert event.access_control.is_public is False
                    assert event.metadata["custom_field"] == "custom_val"
                    assert event.connection == "custom:push"
                    break

        assert message_found, f"Event {event_id} was not found in Redis Stream"


@pytest.mark.asyncio
async def test_custom_data_ingestion_is_actually_consumed_by_enricher(
    setup_tokens: dict[str, str], cleanup_events: AsyncGenerator[None, None]
) -> None:
    """Custom-pushed events must reach a real consumer.

    ingest_custom_data once published a CanonicalEvent to events.raw.chat, which
    takes RawChatPayload only, so every custom event went to the DLQ. This starts
    a real PipelineEnricher subscribed as checkcord/worker.py subscribes it
    (events.raw.custom, no mocked bus.subscribe) and asserts it receives and
    re-publishes the event.
    """
    token_a = "token_push_a"
    headers = {"Authorization": f"Bearer {token_a}"}
    payload = {
        "content_raw": "Customer asked about SSO support",
        "metadata": {"custom_field": "enricher_e2e"},
    }

    with TestClient(app) as client:
        res = client.post("/api/v1/ingest/custom", json=payload, headers=headers)
        assert res.status_code == 201
        event_id = UUID(res.json()["event_id"])

    settings = get_settings()
    redis_client = Redis.from_url(settings.redis_url)
    engine = create_async_engine(settings.database_url)
    enricher = PipelineEnricher(
        bus=RedisStreamsEventBus(redis_client),
        engine=engine,
        group_name="test_enricher_custom_e2e",
        consumer_name="enricher-e2e-1",
    )

    try:
        async with real_subscription(lambda: enricher._subscribe_to_topic("events.raw.custom")):
            found = await wait_for_message_in_stream(
                redis_client,
                "events.enriched.chat",
                lambda fields: fields.get("event_id") == str(event_id),
            )

        assert found, "Custom event was never enriched and republished to events.enriched.chat"

        await assert_topic_not_dlqd(redis_client, "events.raw.custom")
    finally:
        await redis_client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_acl_override_and_tenant_isolation(
    setup_tokens: dict[str, str], cleanup_events: AsyncGenerator[None, None]
) -> None:
    """Verify POST /api/v1/admin/events/{event_id}/acl-override updates db

    and respects tenant isolation.
    """
    token_a = "token_push_a"
    token_b = "token_push_b"
    tenant_a = UUID(setup_tokens[token_a])

    # 1. Insert a test event for Tenant A directly in DB using PostgresEventStore
    # under tenant_context
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    event_id = uuid4()
    original_event = CanonicalEvent(
        event_id=event_id,
        tenant_id=tenant_a,
        source_platform="custom",
        source_type="custom_payload",
        source_id="src_123",
        happened_at=datetime.now(UTC),
        author={"source_id": "author_123"},
        content_raw="Test event content",
        content_clean="Test event content",
        access_control=AccessControl(
            is_public=False,
            visibility=AccessVisibility.PRIVATE,
            acl_type=AclType.CUSTOM,
            allowed_identity_ids=[],
            admin_overrides=[],
        ),
    )

    async with tenant_context(engine, tenant_a) as conn:
        store = PostgresEventStore(conn)
        saved = await store.save_event(original_event)
        assert saved is True

    tenant_b = UUID(setup_tokens[token_b])
    admin_a, admin_b = "token_push_admin_a", "token_push_admin_b"
    admin_override_uuids = [uuid4(), uuid4()]
    override_payload_a = {"admin_overrides": [str(uid) for uid in admin_override_uuids]}
    keys = [KeySpec(admin_a, tenant_a, admin=True), KeySpec(admin_b, tenant_b, admin=True)]

    async with seeded_keys(keys):
        with TestClient(app) as client:
            # An ordinary key of the owning tenant cannot change access.
            res_ordinary = client.post(
                f"/api/v1/admin/events/{event_id}/acl-override",
                json=override_payload_a,
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert res_ordinary.status_code == 403
            assert res_ordinary.json()["detail"] == "Forbidden: admin rights required"

            # Tenant B's admin key gets 404: the query is scoped to (event_id, tenant_id).
            res_isolation = client.post(
                f"/api/v1/admin/events/{event_id}/acl-override",
                json={"admin_overrides": [str(uuid4())]},
                headers={"Authorization": f"Bearer {admin_b}"},
            )
            assert res_isolation.status_code == 404

            res_override = client.post(
                f"/api/v1/admin/events/{event_id}/acl-override",
                json=override_payload_a,
                headers={"Authorization": f"Bearer {admin_a}"},
            )
            assert res_override.status_code == 200

    # 4. Verify DB was updated correctly
    async with tenant_context(engine, tenant_a) as conn:
        store = PostgresEventStore(conn)
        (updated_event,) = await store.get_events_by_ids(tenant_a, [event_id])
        assert updated_event.access_control.admin_overrides == admin_override_uuids

    await engine.dispose()
