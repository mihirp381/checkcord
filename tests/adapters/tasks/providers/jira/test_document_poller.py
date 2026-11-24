from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.adapters.tasks.providers.jira.document_poller import poll_jira_documents
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core.config import get_settings
from checkcord.models.event import CanonicalEvent

pytestmark = pytest.mark.integration

JIRA_HOST = "doc-poller-test.atlassian.net"


async def _drain_redis_stream(redis_url: str, topic: str) -> list[dict[str, Any]]:
    client = Redis.from_url(redis_url)
    try:
        raw_streams: Any = await client.xread(streams={topic: "0-0"}, count=100)
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


async def _flush_redis_stream(redis_url: str, topic: str) -> None:
    client = Redis.from_url(redis_url)
    try:
        await client.delete(topic)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_poll_jira_publishes_the_ticket_under_its_shared_id() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    tenant_id = uuid4()

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text(
                "INSERT INTO jira_installations "
                "(id, jira_host, tenant_id, personal_access_token, webhook_secret) "
                "VALUES (:id, :host, :tid, :pat, :secret)"
            ),
            {
                "id": uuid4(),
                "host": JIRA_HOST,
                "tid": tenant_id,
                "pat": "raw-unencrypted-pat",
                "secret": "webhook-secret",
            },
        )

    redis_client = Redis.from_url(settings.redis_url)
    bus = RedisStreamsEventBus(redis_client)

    fake_issue_payload = {
        "id": "10001",
        "self": f"https://{JIRA_HOST}/rest/api/3/issue/10001",
        "key": "DOC-1",
        "fields": {
            "summary": "Design doc ingestion",
            "description": {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Full issue body text"}],
                    }
                ],
            },
            "status": {"name": "In Progress"},
            "issuetype": {"name": "Task"},
            "project": {"key": "DOC"},
        },
    }

    try:
        with (
            patch(
                "checkcord.adapters.tasks.providers.jira.document_poller._issues_after",
                AsyncMock(
                    return_value=(
                        [{"key": "DOC-1", "fields": {"updated": "2099-01-01T00:00:00.000+0000"}}],
                        False,
                    )
                ),
            ),
            patch(
                "checkcord.adapters.jira_adapter.JiraAdapter.fetch_issue",
                AsyncMock(return_value=fake_issue_payload),
            ),
        ):
            published = await poll_jira_documents(engine, bus)

        assert published == 1

        events = await _drain_redis_stream(settings.redis_url, "events.raw.tasks")
        assert len(events) == 1
        ev = events[0]
        assert ev["tenant_id"] == str(tenant_id)
        assert ev["source_platform"] == "jira"
        assert "Full issue body text" in ev["content_clean"]
        assert ev["source_id"] == f"jira:{JIRA_HOST}:10001:created"
    finally:
        await _flush_redis_stream(settings.redis_url, "events.raw.tasks")
        await redis_client.aclose()
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM jira_installations WHERE jira_host = :host"),
                {"host": JIRA_HOST},
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_poll_jira_documents_falls_back_to_raw_token_on_decrypt_failure() -> None:
    """Pre-existing rows created before Fernet encryption was wired up store the PAT in
    plaintext; the poller must still authenticate with them rather than crash."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    tenant_id = uuid4()

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text(
                "INSERT INTO jira_installations "
                "(id, jira_host, tenant_id, personal_access_token, webhook_secret) "
                "VALUES (:id, :host, :tid, :pat, :secret)"
            ),
            {
                "id": uuid4(),
                "host": JIRA_HOST,
                "tid": tenant_id,
                "pat": "plaintext-pat-not-fernet-encrypted",
                "secret": "webhook-secret",
            },
        )

    try:
        with patch(
            "checkcord.adapters.tasks.providers.jira.document_poller._issues_after",
            AsyncMock(return_value=([], False)),
        ) as mock_fetch_keys:
            published = await poll_jira_documents(engine, AsyncMock())

        assert published == 0
        mock_fetch_keys.assert_awaited_once()
        assert mock_fetch_keys.await_args is not None
        adapter_arg = mock_fetch_keys.await_args.args[0]
        assert adapter_arg.personal_access_token == "plaintext-pat-not-fernet-encrypted"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM jira_installations WHERE jira_host = :host"),
                {"host": JIRA_HOST},
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_poll_jira_documents_swallows_per_installation_failures() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    tenant_id = uuid4()

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text(
                "INSERT INTO jira_installations "
                "(id, jira_host, tenant_id, personal_access_token, webhook_secret) "
                "VALUES (:id, :host, :tid, :pat, :secret)"
            ),
            {
                "id": uuid4(),
                "host": JIRA_HOST,
                "tid": tenant_id,
                "pat": "raw-unencrypted-pat",
                "secret": "webhook-secret",
            },
        )

    try:
        with patch(
            "checkcord.adapters.tasks.providers.jira.document_poller._issues_after",
            AsyncMock(side_effect=Exception("boom")),
        ):
            published = await poll_jira_documents(engine, AsyncMock())
        assert published == 0
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM jira_installations WHERE jira_host = :host"),
                {"host": JIRA_HOST},
            )
        await engine.dispose()
