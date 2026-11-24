import asyncio
import ssl
import typing
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import aiohttp
import aiosqlite
import pytest
import pytest_asyncio
from aiohttp import web
from fastapi.testclient import TestClient
from key_helpers import tenant_keys
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core.config import get_settings
from checkcord.main import app
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.pipeline.ast_linker import ASTLinkerWorker
from checkcord.pipeline.enricher import PipelineEnricher
from checkcord.proxy.config import get_proxy_settings
from checkcord.proxy.ledger import init_ledger_db
from checkcord.proxy.server import LocalProxyServer
from checkcord.storage.postgres import PostgresEventStore, tenant_context

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class MockUpstream:
    def __init__(self) -> None:
        self.app = web.Application()
        self.app.router.add_post("/v1/chat/completions", self.handle_openai)
        self.app.router.add_post("/v1/messages", self.handle_anthropic)
        self.runner: web.AppRunner | None = None
        self.port: int = 0
        self.last_received_payload = None

    async def handle_openai(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.last_received_payload = body
        system_modified = "system" in body or any(
            m["role"] == "system" for m in body.get("messages", [])
        )
        return web.json_response(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": f"Mock OpenAI output. System modified: {system_modified}",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "prompt_tokens_details": {"cached_tokens": 50},
                },
            }
        )

    async def handle_anthropic(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.last_received_payload = body
        return web.json_response(
            {
                "content": [
                    {
                        "type": "text",
                        "text": f"Mock Anthropic output. System value: {body.get('system', '')}",
                    }
                ],
                "usage": {
                    "input_tokens": 250,
                    "output_tokens": 60,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 40,
                },
            }
        )

    async def start(self) -> int:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        assert isinstance(site._server, asyncio.Server)
        self.port = site._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()


@pytest_asyncio.fixture
async def mock_upstream() -> AsyncGenerator[MockUpstream, None]:
    upstream = MockUpstream()
    await upstream.start()
    yield upstream
    await upstream.stop()


@pytest.fixture(scope="module", autouse=True)
def setup_tokens() -> Iterator[dict[str, str]]:
    tenant_a = str(uuid4())
    token_a = "token_proxy_flow_a"
    tokens = {token_a: tenant_a}
    with tenant_keys(tokens):
        yield tokens


@pytest_asyncio.fixture
async def cleanup_db() -> AsyncGenerator[None, None]:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    async def _clean() -> None:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(text("TRUNCATE TABLE ast_context_edges CASCADE"))
            await conn.execute(text("TRUNCATE TABLE ast_nodes CASCADE"))
            await conn.execute(text("TRUNCATE TABLE context_event_links CASCADE"))
            await conn.execute(text("TRUNCATE TABLE canonical_contexts CASCADE"))
            await conn.execute(text("TRUNCATE TABLE events CASCADE"))

    await _clean()
    yield
    await _clean()
    await engine.dispose()


async def test_proxy_warns_about_a_symbol_another_branch_changes(
    tmp_path: Path,
    mock_upstream: MockUpstream,
    cleanup_db: AsyncGenerator[None, None],
    setup_tokens: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ingested rule linked to a symbol reaches the prompt with a branch-conflict warning."""
    tenant_id = UUID(setup_tokens["token_proxy_flow_a"])
    token_val = "token_proxy_flow_a"
    repo_id = "github.com/company/proxy-flow-repo"

    # Setup directories for local proxy database and certs
    cert_dir = tmp_path / "certs"
    db_dir = tmp_path / ".context"
    db_dir.mkdir(parents=True, exist_ok=True)
    ledger_db = str(db_dir / "ledger.db")
    telemetry_db = str(db_dir / "telemetry.db")

    await init_ledger_db(ledger_db)

    # Sync AST Node `calculate_billing` to Postgres via API
    with TestClient(app) as client:
        sync_payload = {
            "repo_identifier": repo_id,
            "nodes": [
                {
                    "file_path": "src/billing.py",
                    "symbol_name": "calculate_billing",
                    "node_type": "function_definition",
                    "merkle_hash": "merkle_bill_123",
                }
            ],
        }
        res_sync = client.post(
            "/api/v1/graph/ast/sync",
            json=sync_payload,
            headers={"Authorization": f"Bearer {token_val}"},
        )
        assert res_sync.status_code == 202

    # Ingest custom context event (Slack rule) via API
    rule_summary = (
        "Warning: calculate_billing must use integer calculations to avoid floating point issues."
    )
    with TestClient(app) as client:
        ingest_payload = {
            "content_raw": rule_summary,
            "metadata": {"type": "architectural_decision"},
        }
        res_ingest = client.post(
            "/api/v1/ingest/custom",
            json=ingest_payload,
            headers={"Authorization": f"Bearer {token_val}"},
        )
        assert res_ingest.status_code == 201
        ingest_data = res_ingest.json()
        event_id = UUID(ingest_data["event_id"])

    # Enrich and link AST via workers
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    bus = RedisStreamsEventBus(redis)

    try:
        # Read the raw event from Redis. /api/v1/ingest/custom publishes to
        # events.raw.custom; events.raw.chat takes RawChatPayload only.
        # redis-py's xread type stub disagrees with its actual list-of-tuples return shape.
        raw_streams: typing.Any = await redis.xread(streams={"events.raw.custom": "0-0"}, count=10)
        assert raw_streams, "No events in events.raw.custom"
        _, messages = raw_streams[0]
        # Find the message we posted
        target_message = None
        for _, fields in messages:
            decoded = {k.decode("utf-8"): v.decode("utf-8") for k, v in fields.items()}
            if decoded.get("event_id") == str(event_id):
                target_message = CanonicalEvent.from_redis_dict(decoded)
                break
        assert target_message is not None, "Target event not found in raw chat stream"

        # Enrich the event
        engine = create_async_engine(settings.database_url)
        enricher = PipelineEnricher(bus=bus, engine=engine)
        await enricher.process_message(target_message)

        # Retrieve the enriched event (which gets published to events.enriched.chat)
        enriched_streams: typing.Any = await redis.xread(
            streams={"events.enriched.chat": "0-0"}, count=10
        )
        assert enriched_streams, "No events in events.enriched.chat"
        _, enriched_messages = enriched_streams[0]
        target_enriched = None
        for _, fields in enriched_messages:
            decoded = {k.decode("utf-8"): v.decode("utf-8") for k, v in fields.items()}
            if decoded.get("event_id") == str(event_id):
                target_enriched = CanonicalEvent.from_redis_dict(decoded)
                break
        assert target_enriched is not None, "Target event not found in enriched chat stream"

        # Now, save it as a canonical context in the DB
        async with tenant_context(engine, tenant_id) as conn:
            await conn.execute(text("SET LOCAL ROLE rls_test_user"))
            store = PostgresEventStore(conn)
            # Create canonical context summary matching the custom push content
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary=rule_summary,
                confidence_score=9.9,
                id=event_id,
                context_type="adr",
            )

        # Run ASTLinkerWorker to link `calculate_billing` symbol to this context
        linker = ASTLinkerWorker(bus=bus, engine=engine)
        linking_event = CanonicalEvent(
            event_id=uuid4(),
            tenant_id=tenant_id,
            source_platform=SourcePlatform.CUSTOM,
            source_type=SourceType.MESSAGE,
            source_id=str(event_id),
            happened_at=datetime.now(UTC),
            author={"source_id": "system"},
            content_raw=rule_summary,
            content_clean=rule_summary,
            access_control={"is_public": True},
        )
        await linker.process_message(linking_event)

        # Simulate Git watcher recording parallel branch modification in SQLite
        async with aiosqlite.connect(ledger_db) as db:
            await db.execute(
                "INSERT INTO active_branches (branch_name, commit_hash) VALUES (?, ?)",
                ("feature/upgrade", "up12345"),
            )
            await db.execute(
                "INSERT INTO modified_files (branch_name, file_path, status) VALUES (?, ?, ?)",
                ("feature/upgrade", "src/billing.py", "M"),
            )
            await db.commit()

        # Start Local Proxy Server
        proxy_settings = get_proxy_settings()
        proxy_settings.cert_dir = str(cert_dir)
        proxy_settings.port = 0
        proxy_settings.host = "127.0.0.1"
        proxy_settings.use_tls = True
        proxy_settings.openai_upstream = f"http://127.0.0.1:{mock_upstream.port}"
        proxy_settings.anthropic_upstream = f"http://127.0.0.1:{mock_upstream.port}"
        proxy_settings.bypass = False
        proxy_settings.database_url = settings.database_url
        proxy_settings.tenant_id = str(tenant_id)

        server = LocalProxyServer()
        server.settings = proxy_settings
        server.git_watcher.db_path = ledger_db
        server.telemetry_store.db_path = telemetry_db
        server.audit_logger.relative_log_dir = str(db_dir / "logs")
        server.git_watcher.current_branch = "main"

        # Set up a dummy ARCHITECTURE.md context file
        arch_file = tmp_path / "ARCHITECTURE.md"
        arch_file.write_text(rule_summary, encoding="utf-8")
        server.context_cache.relative_path = str(arch_file)

        await server.start()
        proxy_port = server.runner.addresses[0][1]
        proxy_url = f"https://127.0.0.1:{proxy_port}"

        # Make an outbound LLM request referencing `calculate_billing`
        payload = {
            "model": "claude-3-5-sonnet",
            "system": "You are refactoring calculate_billing function in src/billing.py.",
            "messages": [{"role": "user", "content": "Update the billing logic."}],
        }
        headers = {"Authorization": "Bearer test-key"}
        ssl_context = ssl.create_default_context(cafile=str(cert_dir / "ca.pem"))

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{proxy_url}/v1/messages", json=payload, headers=headers, ssl=ssl_context
            ) as resp:
                assert resp.status == 200
                resp_data = await resp.json()
                assert "Mock Anthropic output" in resp_data["content"][0]["text"]

        # Verify warning and context injection in Upstream payload
        upstream_payload = mock_upstream.last_received_payload
        assert upstream_payload is not None
        injected_system = upstream_payload.get("system")
        assert injected_system is not None
        assert rule_summary in injected_system
        assert "<conflict_warning>" in injected_system
        assert (
            "WARNING: Another active local branch (feature/upgrade) is modifying" in injected_system
        )
        assert "calculate_billing" in injected_system

        # Cleanup
        await server.stop()
        await engine.dispose()
    finally:
        await redis.aclose()
