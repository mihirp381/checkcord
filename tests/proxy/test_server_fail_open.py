import asyncio
import ssl
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast
from uuid import uuid4

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from checkcord.proxy.config import get_proxy_settings
from checkcord.proxy.ledger import init_ledger_db
from checkcord.proxy.server import LocalProxyServer


class MockUpstream:
    def __init__(self) -> None:
        self.app = web.Application()
        self.app.router.add_post("/v1/messages", self.handle_anthropic)
        self.runner: web.AppRunner | None = None
        self.port = 0

    async def handle_anthropic(self, request: web.Request) -> web.Response:
        await request.json()
        return web.json_response(
            {
                "content": [{"type": "text", "text": "Mock Anthropic output."}],
                "usage": {"input_tokens": 150, "output_tokens": 50},
            }
        )

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        server = cast(asyncio.Server, site._server)
        self.port = server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()


@pytest_asyncio.fixture
async def mock_upstream() -> AsyncGenerator[MockUpstream, None]:
    upstream = MockUpstream()
    await upstream.start()
    yield upstream
    await upstream.stop()


@pytest.mark.asyncio
async def test_proxy_forwards_when_the_database_is_unreachable(
    tmp_path: Path, mock_upstream: MockUpstream
) -> None:
    db_dir = tmp_path / ".context"
    db_dir.mkdir(parents=True)
    ledger_db = str(db_dir / "ledger.db")
    await init_ledger_db(ledger_db)
    cert_dir = tmp_path / "certs"

    proxy_settings = get_proxy_settings()
    proxy_settings.cert_dir = str(cert_dir)
    proxy_settings.port = 0
    proxy_settings.host = "127.0.0.1"
    proxy_settings.use_tls = True
    proxy_settings.openai_upstream = f"http://127.0.0.1:{mock_upstream.port}"
    proxy_settings.anthropic_upstream = f"http://127.0.0.1:{mock_upstream.port}"
    proxy_settings.bypass = False
    proxy_settings.database_url = (
        "postgresql+asyncpg://checkcord:checkcord@127.0.0.1:54321/non_existent_db"
    )
    proxy_settings.tenant_id = str(uuid4())

    server = LocalProxyServer()
    server.settings = proxy_settings
    server.git_watcher.db_path = ledger_db
    server.telemetry_store.db_path = str(db_dir / "telemetry.db")
    server.audit_logger.relative_log_dir = str(tmp_path / "logs")

    await server.start()
    proxy_url = f"https://127.0.0.1:{server.runner.addresses[0][1]}"
    ssl_context = ssl.create_default_context(cafile=str(cert_dir / "ca.pem"))
    payload = {
        "model": "claude-3-5-sonnet",
        "system": "You are a software engineer.",
        "messages": [{"role": "user", "content": "Hello!"}],
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{proxy_url}/v1/messages",
                json=payload,
                headers={"Authorization": "Bearer sim-auth-key"},
                ssl=ssl_context,
            ) as resp:
                assert resp.status == 200
                assert "Mock Anthropic output" in (await resp.json())["content"][0]["text"]
    finally:
        await server.stop()
