import json
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from key_helpers import tenant_keys
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core.config import get_settings
from checkcord.main import app
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.pipeline.ast_linker import ASTLinkerWorker
from checkcord.pipeline.enricher import PipelineEnricher

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="module", autouse=True)
def setup_integration_tokens() -> Iterator[dict[str, str]]:
    tenant_id = str(uuid4())
    token = "token_agent_ast_integration"
    tokens = {token: tenant_id}
    with tenant_keys(tokens):
        yield tokens


@pytest_asyncio.fixture
async def setup_db_and_redis() -> AsyncGenerator[dict[str, Any], None]:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    redis = Redis.from_url(settings.redis_url)
    bus = RedisStreamsEventBus(redis)

    # Clean up streams
    await redis.delete("events.raw.agent")
    await redis.delete("events.enriched.agent")

    yield {"engine": engine, "redis": redis, "bus": bus}

    await redis.delete("events.raw.agent")
    await redis.delete("events.enriched.agent")
    await redis.aclose()
    await engine.dispose()


async def test_agent_trajectory_to_ast_linking_flow(
    setup_integration_tokens: dict[str, str],
    setup_db_and_redis: dict[str, Any],
) -> None:
    """Integration test validating:
    1. Agent trajectory submission via API.
    2. Redis events.raw.agent stream receives the CanonicalEvent.
    3. PipelineEnricher processes raw event and pushes to events.enriched.agent.
    4. AST Linker processes the context summary and matches symbols in the DB.
    """
    engine = setup_db_and_redis["engine"]
    redis = setup_db_and_redis["redis"]
    bus = setup_db_and_redis["bus"]
    token = "token_agent_ast_integration"
    tenant_id = UUID(setup_integration_tokens[token])

    # Insert an AST node and a mock canonical context
    # Representing a symbol we want the agent trajectory to link to.
    ast_node_id = uuid4()
    context_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        # Clear any old data
        await conn.execute(
            text("DELETE FROM ast_context_edges WHERE tenant_id = :tid"), {"tid": tenant_id}
        )
        await conn.execute(text("DELETE FROM ast_nodes WHERE tenant_id = :tid"), {"tid": tenant_id})
        await conn.execute(
            text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
        )

        # Insert AST Node for 'auth_service'
        await conn.execute(
            text(
                "INSERT INTO ast_nodes (id, tenant_id, repo_identifier, file_path, "
                "symbol_name, node_type, merkle_hash) "
                "VALUES (:id, :tenant_id, 'org/repo', 'src/auth.py', 'auth_service', "
                "'function_definition', 'hash123')"
            ),
            {"id": ast_node_id, "tenant_id": tenant_id},
        )

        # Insert Canonical Context (representing the parsed decision summary)
        await conn.execute(
            text(
                "INSERT INTO canonical_contexts (id, tenant_id, summary, confidence_score) "
                "VALUES (:id, :tenant_id, 'Reference for auth_service configuration details.', 9.5)"
            ),
            {"id": context_id, "tenant_id": tenant_id},
        )

    # 1. Post trajectory via client API
    payload = {
        "agent_name": "ast-test-agent",
        "session_id": "sess-integration-ast",
        "repository": "org/repo",
        "task_description": "Initialize the auth_service configuration",
        "raw_transcript": "I need to initialize the auth_service configuration "
        "inside the login routine.",
    }

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/agents/trajectory",
            content=json.dumps(payload),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 202
    resp_data = resp.json()
    _ = resp_data["event_id"]

    # 2. Assert raw event was published
    raw_streams: Any = await redis.xread(streams={"events.raw.agent": "0-0"}, count=10)
    assert raw_streams, "No messages in events.raw.agent stream"

    # 3. Process the raw event via PipelineEnricher
    enricher = PipelineEnricher(
        bus=bus,
        engine=engine,
        group_name="test_enrich_group",
        consumer_name="test_enrich_worker",
    )

    # Decode and reconstruct the CanonicalEvent from Redis stream
    _, messages = raw_streams[0]
    _, fields = messages[0]
    decoded_fields = {k.decode("utf-8"): v.decode("utf-8") for k, v in fields.items()}
    raw_event = CanonicalEvent.from_redis_dict(decoded_fields)

    # Run the enrichment
    await enricher.process_message(raw_event)

    # Read from enriched stream
    enriched_streams: Any = await redis.xread(streams={"events.enriched.agent": "0-0"}, count=10)
    assert enriched_streams, "No messages in events.enriched.agent stream"
    _, enriched_messages = enriched_streams[0]
    _, enriched_fields = enriched_messages[0]
    decoded_enriched = {k.decode("utf-8"): v.decode("utf-8") for k, v in enriched_fields.items()}
    enriched_event = CanonicalEvent.from_redis_dict(decoded_enriched)

    # Ensure source platform is correct
    assert enriched_event.source_platform == SourcePlatform.AGENT_SESSION
    assert enriched_event.source_type == SourceType.AGENT_LOG

    # 4. AST Linker processes the context summary and maps symbol
    # Normally ASTLinkerWorker runs on contexts.created. Since our agent trajectory
    # has generated a canonical context or maps symbols, let's verify ASTLinkerWorker
    # can link this CanonicalEvent (simulating the context created event) to the DB node.
    # Note: ASTLinkerWorker.process_message expects event.source_id to be a valid UUID.
    # Let's map our event with context_id as source_id to verify the linking.
    linking_event = CanonicalEvent(
        event_id=uuid4(),
        tenant_id=tenant_id,
        source_platform=SourcePlatform.AGENT_SESSION,
        source_type=SourceType.AGENT_LOG,
        source_id=str(context_id),
        happened_at=datetime.now(UTC),
        author=AuthorInfo(source_id="agent"),
        content_raw="Reference for auth_service configuration details.",
        content_clean="Reference for auth_service configuration details.",
        access_control=AccessControl(is_public=True),
    )

    linker = ASTLinkerWorker(bus=bus, engine=engine)
    await linker.process_message(linking_event)

    # Verify that the linkage is recorded in the postgres database
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        result = await conn.execute(
            text("SELECT ast_node_id, canonical_id FROM ast_context_edges WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
        rows = result.fetchall()

    assert len(rows) == 1
    assert rows[0][0] == ast_node_id
    assert rows[0][1] == context_id
