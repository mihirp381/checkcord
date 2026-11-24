import hashlib
import hmac
import json
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from pubsub_test_helpers import assert_topic_not_dlqd, real_subscription, wait_for_message_in_stream
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core.config import get_settings
from checkcord.main import app
from checkcord.models.event import CanonicalEvent
from checkcord.pipeline.enricher import PipelineEnricher

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JIRA_HOST = "test-company.atlassian.net"
WEBHOOK_SECRET = "test_jira_webhook_secret"
PAT = "test_jira_pat"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signature(payload_bytes: bytes, secret: str = WEBHOOK_SECRET) -> str:
    """Return the ``sha256=…`` HMAC digest for a payload."""
    digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _headers(payload_bytes: bytes, secret: str = WEBHOOK_SECRET) -> dict[str, str]:
    return {
        "X-Hub-Signature-256": _make_signature(payload_bytes, secret),
        "Content-Type": "application/json",
    }


async def _drain_redis_stream(redis_url: str) -> list[dict[str, Any]]:
    """Read all messages from ``events.raw.tasks`` and return decoded event dicts."""
    client = Redis.from_url(redis_url)
    try:
        raw_streams: Any = await client.xread(streams={"events.raw.tasks": "0-0"}, count=100)
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


async def _flush_redis_stream(redis_url: str) -> None:
    client = Redis.from_url(redis_url)
    try:
        await client.delete("events.raw.tasks")
    except Exception:
        pass
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def jira_installation() -> Any:
    """Insert a JiraInstallation row plus supporting user rows."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    tenant_id = uuid4()
    ent_id = uuid4()

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text(
                "INSERT INTO enterprise_users "
                "(id, tenant_id, primary_email, display_name, active) "
                "VALUES (:id, :tenant_id, 'dev@company.example', 'Jira Developer', True)"
            ),
            {"id": ent_id, "tenant_id": tenant_id},
        )
        await conn.execute(
            text(
                "INSERT INTO chat_users "
                "(id, tenant_id, platform, native_id, display_name, email, internal_id) "
                "VALUES (:id, :tenant_id, 'jira', 'jira-user-abc', "
                "'Jira Developer', 'dev@company.example', :internal_id)"
            ),
            {"id": uuid4(), "tenant_id": tenant_id, "internal_id": ent_id},
        )
        await conn.execute(
            text(
                "INSERT INTO jira_installations "
                "(id, jira_host, tenant_id, personal_access_token, webhook_secret) "
                "VALUES (:id, :host, :tenant_id, :pat, :secret)"
            ),
            {
                "id": uuid4(),
                "host": JIRA_HOST,
                "tenant_id": tenant_id,
                "pat": PAT,
                "secret": WEBHOOK_SECRET,
            },
        )

    yield {"tenant_id": tenant_id, "ent_id": ent_id}

    # Teardown
    await _flush_redis_stream(settings.redis_url)
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text("DELETE FROM jira_installations WHERE jira_host = :host"),
            {"host": JIRA_HOST},
        )
        await conn.execute(
            text("DELETE FROM chat_users WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
        await conn.execute(
            text("DELETE FROM enterprise_users WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
    await engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jira_webhook_ping(jira_installation: dict[str, Any]) -> None:
    payload = {
        "webhookEvent": "webhook_test",
        "timestamp": 1719225840000,
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes)

    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/jira", content=payload_bytes, headers=hdrs)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ping_ok"}


@pytest.mark.asyncio
async def test_jira_webhook_invalid_signature(jira_installation: dict[str, Any]) -> None:
    payload = {
        "webhookEvent": "jira:issue_created",
        "timestamp": 1719225840000,
        "issue": {
            "id": "10002",
            "key": "ENG-42",
            "self": f"https://{JIRA_HOST}/rest/api/2/issue/10002",
            "fields": {
                "summary": "Fix auth loop",
                "status": {"name": "To Do"},
            },
        },
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes, secret="wrong_secret")

    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/jira", content=payload_bytes, headers=hdrs)

    assert response.status_code == 401
    assert "Invalid signature" in response.text


@pytest.mark.asyncio
async def test_jira_webhook_unknown_host(jira_installation: dict[str, Any]) -> None:
    payload = {
        "webhookEvent": "jira:issue_created",
        "timestamp": 1719225840000,
        "issue": {
            "id": "10002",
            "key": "ENG-42",
            "self": "https://unknown.atlassian.net/rest/api/2/issue/10002",
            "fields": {
                "summary": "Fix auth loop",
                "status": {"name": "To Do"},
            },
        },
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes)

    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/jira", content=payload_bytes, headers=hdrs)

    assert response.status_code == 401
    assert "Jira host not installed" in response.text


@pytest.mark.asyncio
async def test_jira_webhook_issue_created(jira_installation: dict[str, Any]) -> None:
    payload = {
        "timestamp": 1719225840000,
        "webhookEvent": "jira:issue_created",
        "user": {
            "accountId": "jira-user-abc",
            "displayName": "Jira Developer",
            "emailAddress": "dev@company.example",
        },
        "issue": {
            "id": "10002",
            "key": "ENG-42",
            "self": f"https://{JIRA_HOST}/rest/api/2/issue/10002",
            "fields": {
                "summary": "Fix auth loop",
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "This is blocking dev. Check PR: https://github.com/my-org/my-repo/pull/425.",
                                }
                            ],
                        }
                    ],
                },
                "status": {"name": "To Do"},
                "issuetype": {"id": "1", "name": "Bug", "subtask": False},
            },
        },
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes)

    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/jira", content=payload_bytes, headers=hdrs)

    assert response.status_code == 202, response.text

    # Verify Redis publication
    settings = get_settings()
    events = await _drain_redis_stream(settings.redis_url)
    assert len(events) == 1

    ev = events[0]
    assert ev["source_platform"] == "jira"
    assert ev["source_type"] == "ticket_created"
    assert ev["event_action"] == "created"
    assert ev["tenant_id"] == str(jira_installation["tenant_id"])
    assert ev["author"]["internal_id"] == str(jira_installation["ent_id"])
    assert "ENG-42" in ev["content_clean"]
    assert "https://github.com/my-org/my-repo/pull/425" in ev["content_clean"]
    assert len(ev["entities"]) == 2  # GitHub PR and ENG-42 key itself

    # Assert entities are correct
    entities = ev["entities"]
    assert any(
        e["entity_type"] == "pr" and e["entity_id"] == "my-org/my-repo/pull/425" for e in entities
    )
    assert any(e["entity_type"] == "ticket" and e["entity_id"] == "ENG-42" for e in entities)


@pytest.mark.asyncio
async def test_jira_webhook_issue_created_is_actually_consumed_by_enricher(
    jira_installation: dict[str, Any],
) -> None:
    """Jira ticket events must reach a real consumer.

    They were once published to events.raw.chat, which has no CanonicalEvent
    consumer, and were lost. This starts a real PipelineEnricher subscribed as
    checkcord/worker.py subscribes it (events.raw.tasks, no mocked
    bus.subscribe) and asserts it receives and re-publishes the event.
    """
    payload = {
        "timestamp": 1719225840000,
        "webhookEvent": "jira:issue_created",
        "user": {
            "accountId": "jira-user-abc",
            "displayName": "Jira Developer",
            "emailAddress": "dev@company.example",
        },
        "issue": {
            "id": "10003",
            "key": "ENG-43",
            "self": f"https://{JIRA_HOST}/rest/api/2/issue/10003",
            "fields": {
                "summary": "Fix session timeout",
                "status": {"name": "To Do"},
                "issuetype": {"id": "1", "name": "Bug", "subtask": False},
            },
        },
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes)

    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/jira", content=payload_bytes, headers=hdrs)

    assert response.status_code == 202, response.text
    event_id = response.json()["event_ids"][0]

    settings = get_settings()
    redis_client = Redis.from_url(settings.redis_url)
    engine = create_async_engine(settings.database_url)
    enricher = PipelineEnricher(
        bus=RedisStreamsEventBus(redis_client),
        engine=engine,
        group_name="test_enricher_jira_tasks_e2e",
        consumer_name="enricher-jira-e2e-1",
    )

    try:
        async with real_subscription(lambda: enricher._subscribe_to_topic("events.raw.tasks")):
            found = await wait_for_message_in_stream(
                redis_client,
                "events.enriched.chat",
                lambda fields: fields.get("event_id") == event_id,
            )

        assert found, "Jira ticket event was never enriched and republished to events.enriched.chat"

        await assert_topic_not_dlqd(redis_client, "events.raw.tasks")
    finally:
        await redis_client.delete("events.enriched.chat", "events.raw.tasks.dlq")
        await redis_client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_jira_webhook_issue_status_updated(jira_installation: dict[str, Any]) -> None:
    payload = {
        "timestamp": 1719225840000,
        "webhookEvent": "jira:issue_updated",
        "user": {
            "accountId": "jira-user-abc",
            "displayName": "Jira Developer",
            "emailAddress": "dev@company.example",
        },
        "issue": {
            "id": "10002",
            "key": "ENG-42",
            "self": f"https://{JIRA_HOST}/rest/api/2/issue/10002",
            "fields": {
                "summary": "Fix auth loop",
                "description": None,
                "status": {"name": "Done"},
                "issuetype": {"id": "1", "name": "Bug", "subtask": False},
            },
        },
        "changelog": {
            "id": "20005",
            "items": [
                {
                    "field": "status",
                    "fieldId": "status",
                    "from": "3",
                    "fromString": "In Progress",
                    "to": "10000",
                    "toString": "Done",
                }
            ],
        },
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes)

    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/jira", content=payload_bytes, headers=hdrs)

    assert response.status_code == 202, response.text

    settings = get_settings()
    events = await _drain_redis_stream(settings.redis_url)
    assert len(events) == 1

    ev = events[0]
    assert ev["source_platform"] == "jira"
    # Status change maps to SourceType.TASK as per user comment
    assert ev["source_type"] == "task"
    assert ev["event_action"] == "updated"
    assert "Changed status from 'In Progress' to 'Done'" in ev["content_clean"]


@pytest.mark.asyncio
async def test_jira_webhook_comment_created(jira_installation: dict[str, Any]) -> None:
    payload = {
        "timestamp": 1719225840000,
        "webhookEvent": "comment_created",
        "comment": {
            "id": "30001",
            "self": f"https://{JIRA_HOST}/rest/api/2/issue/10002/comment/30001",
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "This is a comment. Link to Confluence: https://my-company.atlassian.net/wiki/spaces/ENG/pages/1039405/Design+Doc",
                            }
                        ],
                    }
                ],
            },
            "author": {
                "accountId": "jira-user-abc",
                "displayName": "Jira Developer",
                "emailAddress": "dev@company.example",
            },
            "created": "2024-06-24T10:44:00.000+0000",
        },
        "issue": {
            "id": "10002",
            "key": "ENG-42",
            "self": f"https://{JIRA_HOST}/rest/api/2/issue/10002",
            "fields": {
                "summary": "Fix auth loop",
                "status": {"name": "To Do"},
            },
        },
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes)

    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/jira", content=payload_bytes, headers=hdrs)

    assert response.status_code == 202, response.text

    settings = get_settings()
    events = await _drain_redis_stream(settings.redis_url)
    assert len(events) == 1

    ev = events[0]
    assert ev["source_platform"] == "jira"
    assert ev["source_type"] == "ticket_comment"
    assert ev["event_action"] == "created"
    assert "This is a comment." in ev["content_clean"]
    assert any(
        e["entity_type"] == "wiki_page" and "spaces/ENG/pages/1039405/Design+Doc" in e["entity_id"]
        for e in ev["entities"]
    )


async def test_jira_redelivery_is_published_once(jira_installation: dict[str, Any]) -> None:
    payload = {
        "timestamp": 1719225840000,
        "webhookEvent": "jira:issue_created",
        "user": {"accountId": "jira-user-abc", "displayName": "Jira Developer"},
        "issue": {
            "id": "10099",
            "key": "ENG-99",
            "self": f"https://{JIRA_HOST}/rest/api/2/issue/10099",
            "fields": {"summary": "Once", "status": {"name": "To Do"}},
        },
    }
    body = json.dumps(payload).encode()
    hdrs = {**_headers(body), "X-Atlassian-Webhook-Identifier": f"delivery-{uuid4()}"}
    with TestClient(app) as client:
        first = client.post("/api/v1/webhooks/jira", content=body, headers=hdrs)
        again = client.post("/api/v1/webhooks/jira", content=body, headers=hdrs)
    assert first.status_code == 202
    assert again.status_code == 200 and again.json() == {"status": "duplicate"}
    events = await _drain_redis_stream(get_settings().redis_url)
    assert len([e for e in events if "ENG-99" in e["content_clean"]]) == 1
