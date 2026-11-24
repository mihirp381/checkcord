from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.adapters.ingestion.cross_linker import extract_cross_links
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core.config import get_settings
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.event import CanonicalEvent
from checkcord.pipeline.enricher import PipelineEnricher

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture
async def setup_jira_data() -> AsyncGenerator[dict[str, Any], None]:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    tenant_id = uuid4()
    inst_id = uuid4()

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        # Insert Jira Installation
        await conn.execute(
            text(
                "INSERT INTO jira_installations "
                "(id, jira_host, tenant_id, personal_access_token, webhook_secret) "
                "VALUES (:id, :host, :tenant_id, 'pat', 'secret')"
            ),
            {"id": inst_id, "host": "test-company.atlassian.net", "tenant_id": tenant_id},
        )
        # Insert configured Project Keys
        await conn.execute(
            text(
                "INSERT INTO jira_project_keys (id, tenant_id, jira_installation_id, project_key) "
                "VALUES (:id, :tenant_id, :inst_id, :key)"
            ),
            {"id": uuid4(), "tenant_id": tenant_id, "inst_id": inst_id, "key": "ENG"},
        )
        await conn.execute(
            text(
                "INSERT INTO jira_project_keys (id, tenant_id, jira_installation_id, project_key) "
                "VALUES (:id, :tenant_id, :inst_id, :key)"
            ),
            {"id": uuid4(), "tenant_id": tenant_id, "inst_id": inst_id, "key": "PROJ"},
        )

    yield {"tenant_id": tenant_id, "engine": engine}

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text("DELETE FROM jira_project_keys WHERE tenant_id = :tid"), {"tid": tenant_id}
        )
        await conn.execute(
            text("DELETE FROM jira_installations WHERE tenant_id = :tid"), {"tid": tenant_id}
        )
    await engine.dispose()


async def test_extract_cross_links_no_db(setup_jira_data: dict[str, Any]) -> None:
    engine = setup_jira_data["engine"]
    tenant_id = setup_jira_data["tenant_id"]

    # Test text containing all sorts of links
    text_content = (
        "Check out this PR: https://github.com/my-org/my-repo/pull/42 and the corresponding "
        "issue: https://github.com/my-org/my-repo/issues/100. Also see the commit "
        "https://github.com/my-org/my-repo/commit/a1b2c3d4e5f67890. Here is the Sentry trace "
        "and Datadog incident: https://app.datadoghq.com/incidents/789. Also Confluence page "
        "https://my-company.atlassian.net/wiki/spaces/ENG/pages/102030/DesignDoc."
    )

    refs = await extract_cross_links(text_content, tenant_id, engine)

    # Convert refs to a simple dict for easier assertions
    ref_map = {(r.entity_type, r.entity_platform): r.entity_id for r in refs}

    # Assertions
    assert ("pr", "github") in ref_map
    assert ref_map[("pr", "github")] == "my-org/my-repo/pull/42"

    assert ("issue", "github") in ref_map
    assert ref_map[("issue", "github")] == "my-org/my-repo/issues/100"

    assert ("commit", "github") in ref_map
    assert ref_map[("commit", "github")] == "my-org/my-repo/commit/a1b2c3d4e5f67890"

    assert ("incident", "datadog") in ref_map
    assert ref_map[("incident", "datadog")] == "789"

    assert ("wiki", "confluence") in ref_map
    assert ref_map[("wiki", "confluence")] == "ENG/pages/102030"


async def test_extract_jira_project_keys(setup_jira_data: dict[str, Any]) -> None:
    engine = setup_jira_data["engine"]
    tenant_id = setup_jira_data["tenant_id"]

    # ENG-123 is registered, PROJ-999 is registered, BUT FAKE-456 and IT-888 are not!
    text_content = (
        "This resolves ENG-123 and is related to PROJ-999. Do not match FAKE-456 or IT-888."
    )

    refs = await extract_cross_links(text_content, tenant_id, engine)
    tickets = {r.entity_id for r in refs if r.entity_type == "ticket"}

    assert "ENG-123" in tickets
    assert "PROJ-999" in tickets
    assert "FAKE-456" not in tickets
    assert "IT-888" not in tickets


async def test_pipeline_enricher_e2e(setup_jira_data: dict[str, Any]) -> None:
    engine = setup_jira_data["engine"]
    tenant_id = setup_jira_data["tenant_id"]
    settings = get_settings()

    redis = Redis.from_url(settings.redis_url)
    bus = RedisStreamsEventBus(redis)

    try:
        # Flush the streams
        await redis.delete("events.raw.chat")
        await redis.delete("events.enriched.chat")

        # 1. Setup mock event in raw stream
        mock_event = CanonicalEvent(
            tenant_id=tenant_id,
            source_platform="slack",
            source_type="message",
            source_id="msg_abc123",
            happened_at=datetime.now(UTC),
            author=AuthorInfo(source_id="U_slack"),
            content_raw="Hey, please check ENG-123 and https://github.com/my-org/my-repo/pull/1",
            content_clean="Hey, please check ENG-123 and https://github.com/my-org/my-repo/pull/1",
            access_control=AccessControl(is_public=True),
        )

        await bus.publish("events.raw.chat", mock_event)

        # 2. Run PipelineEnricher for one message
        enricher = PipelineEnricher(
            bus, engine, group_name="test_enrichers", consumer_name="enricher-test"
        )

        # Let's mock the process_message execution directly to make it synchronous
        await enricher.process_message(mock_event)

        # 3. Read from enriched stream
        raw_streams: Any = await redis.xread(streams={"events.enriched.chat": "0-0"}, count=100)
        assert raw_streams, "Enriched event was not published"

        _, messages = raw_streams[0]
        assert len(messages) == 1
        _, fields = messages[0]

        decoded = {k.decode("utf-8"): v.decode("utf-8") for k, v in fields.items()}
        enriched_event = CanonicalEvent.from_redis_dict(decoded)

        # 4. Verify entities are populated
        entities = enriched_event.entities
        assert len(entities) == 2

        ref_map = {(r.entity_type, r.entity_platform): r.entity_id for r in entities}
        assert ("ticket", "jira") in ref_map
        assert ref_map[("ticket", "jira")] == "ENG-123"

        assert ("pr", "github") in ref_map
        assert ref_map[("pr", "github")] == "my-org/my-repo/pull/1"

    finally:
        await redis.delete("events.raw.chat")
        await redis.delete("events.enriched.chat")
        await redis.aclose()
