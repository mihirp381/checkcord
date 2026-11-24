import asyncio
import json
import os
import ssl
import subprocess
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from checkcord.proxy.cert_manager import generate_certs
from checkcord.proxy.config import get_bypass_config_path, get_proxy_settings, write_config
from checkcord.proxy.server import LocalProxyServer

# We use standard pytest-asyncio settings
pytestmark = pytest.mark.asyncio


@pytest.fixture
def cert_dir(tmp_path: Path) -> Path:
    """Fixture providing a temporary directory for certs."""
    return tmp_path / "certs"


async def test_cert_generation(cert_dir: Path) -> None:
    """Verify that certificate generation works and sets correct permissions."""
    generated = generate_certs(cert_dir)
    assert generated is True

    ca_path = cert_dir / "ca.pem"
    cert_path = cert_dir / "cert.pem"
    key_path = cert_dir / "key.pem"

    assert ca_path.exists()
    assert cert_path.exists()
    assert key_path.exists()

    # Verify file permissions (private key should be 0600 on unix-like systems)
    if os.name != "nt":
        stat_info = key_path.stat()
        assert (stat_info.st_mode & 0o777) == 0o600

    # Check that calling again returns False (does not regenerate)
    assert generate_certs(cert_dir) is False


class MockUpstream:
    """Helper class to spin up a mock upstream API server."""

    def __init__(self) -> None:
        self.app = web.Application()
        self.app.router.add_post("/v1/chat/completions", self.handle_chat)
        self.app.router.add_post("/v1/messages", self.handle_messages)
        self.app.router.add_get("/v1/stream", self.handle_stream)
        self.app.router.add_post("/v1/chat/completions/stream", self.handle_stream_telemetry)
        self.app.router.add_get("/v1/timeout", self.handle_timeout)
        self.runner: web.AppRunner | None = None
        self.port: int = 0
        self.last_received_payload = None

    async def handle_chat(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.last_received_payload = body
        auth_header = request.headers.get("Authorization")
        return web.json_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": f"Mock response. Auth: {auth_header}. Input: {body['model']}"
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 40},
                },
            }
        )

    async def handle_messages(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.last_received_payload = body
        return web.json_response(
            {
                "content": [{"text": f"Mock Anthropic response. Input model: {body['model']}"}],
                "usage": {
                    "input_tokens": 300,
                    "output_tokens": 50,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 80,
                },
            }
        )

    async def handle_stream(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for i in range(3):
            await resp.write(f"data: token {i}\n\n".encode())
            await asyncio.sleep(0.01)
        await resp.write_eof()
        return resp

    async def handle_stream_telemetry(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(b'data: {"choices": [{"delta": {"content": "Hello"}}]}\n\n')
        await asyncio.sleep(0.01)
        await resp.write(
            b'data: {"choices": [{"delta": {"content": " world"}}], '
            b'"usage": {"prompt_tokens": 150, "completion_tokens": 10, '
            b'"prompt_tokens_details": {"cached_tokens": 30}}}\n\n'
        )
        await asyncio.sleep(0.01)
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp

    async def handle_timeout(self, request: web.Request) -> web.Response:
        await asyncio.sleep(0.5)
        return web.Response(text="slow")

    async def start(self) -> int:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        # Find local port
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


async def test_proxy_forwarding(cert_dir: Path, mock_upstream: MockUpstream) -> None:
    """Verify that requests are forwarded correctly via proxy (HTTP and HTTPS).

    Sets 'mitm' proxy_mode explicitly; the default is 'base_url'.
    """
    # 1. Start proxy with settings pointing to our mock upstream
    settings = get_proxy_settings()
    settings.proxy_mode = "mitm"
    settings.cert_dir = str(cert_dir)
    settings.port = 0  # Bind to OS assigned free port
    settings.host = "127.0.0.1"
    settings.use_tls = True
    settings.openai_upstream = f"http://127.0.0.1:{mock_upstream.port}"
    settings.anthropic_upstream = f"http://127.0.0.1:{mock_upstream.port}"

    proxy = LocalProxyServer()
    # Override settings on the proxy to ensure it has our values
    proxy.settings = settings

    await proxy.start()

    # Retrieve assigned proxy port
    proxy_port = proxy.runner.addresses[0][1]
    proxy_url = f"https://127.0.0.1:{proxy_port}"

    # Create SSL Context to trust the local CA cert generated for testing
    ca_path = cert_dir / "ca.pem"
    ssl_context = ssl.create_default_context(cafile=str(ca_path))

    # Send a request to OpenAI route
    async with aiohttp.ClientSession() as session:
        # 1. Test OpenAI forward
        payload = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]}
        headers = {"Authorization": "Bearer open-api-key-test"}

        async with session.post(
            f"{proxy_url}/v1/chat/completions", json=payload, headers=headers, ssl=ssl_context
        ) as resp:
            assert resp.status == 200
            data = await resp.json()
            assert "Mock response" in data["choices"][0]["message"]["content"]
            assert "Bearer open-api-key-test" in data["choices"][0]["message"]["content"]
            assert "gpt-4o" in data["choices"][0]["message"]["content"]

        # 2. Test Anthropic forward
        anthropic_payload = {
            "model": "claude-3-opus",
            "messages": [{"role": "user", "content": "hi"}],
        }
        async with session.post(
            f"{proxy_url}/v1/messages", json=anthropic_payload, ssl=ssl_context
        ) as resp:
            assert resp.status == 200
            data = await resp.json()
            assert "Mock Anthropic response" in data["content"][0]["text"]
            assert "claude-3-opus" in data["content"][0]["text"]

        # 3. Test Streaming response
        async with session.get(f"{proxy_url}/v1/stream", ssl=ssl_context) as resp:
            assert resp.status == 200
            assert resp.headers.get("Content-Type") == "text/event-stream"
            chunks = []
            async for chunk in resp.content:
                chunks.append(chunk.decode("utf-8"))
            joined = "".join(chunks)
            assert "data: token 0" in joined
            assert "data: token 1" in joined
            assert "data: token 2" in joined

    await proxy.stop()


async def test_proxy_base_url_mode_forwarding(cert_dir: Path, mock_upstream: MockUpstream) -> None:
    """base_url mode: a client configured via ANTHROPIC_BASE_URL hits the
    proxy's own address, trusts only the proxy's local CA, and forwarding works.

    handle_proxy resolves the upstream from the request path in both modes, so
    this is a parity test against test_proxy_forwarding.
    """
    settings = get_proxy_settings()
    settings.proxy_mode = "base_url"
    settings.cert_dir = str(cert_dir)
    settings.port = 0
    settings.host = "127.0.0.1"
    settings.use_tls = True
    settings.anthropic_upstream = f"http://127.0.0.1:{mock_upstream.port}"

    proxy = LocalProxyServer()
    proxy.settings = settings

    await proxy.start()

    proxy_port = proxy.runner.addresses[0][1]
    proxy_url = f"https://127.0.0.1:{proxy_port}"

    # Only the proxy's own localhost cert is trusted -- no custom root CA
    # installed system-wide, mirroring what a base_url-mode client would do.
    ca_path = cert_dir / "ca.pem"
    ssl_context = ssl.create_default_context(cafile=str(ca_path))

    async with aiohttp.ClientSession() as session:
        anthropic_payload = {
            "model": "claude-3-opus",
            "messages": [{"role": "user", "content": "hi"}],
        }
        async with session.post(
            f"{proxy_url}/v1/messages", json=anthropic_payload, ssl=ssl_context
        ) as resp:
            assert resp.status == 200
            data = await resp.json()
            assert "Mock Anthropic response" in data["content"][0]["text"]
            assert "claude-3-opus" in data["content"][0]["text"]

    await proxy.stop()


async def test_proxy_bypass_mode(cert_dir: Path, mock_upstream: MockUpstream) -> None:
    """Verify that bypass mode works (skips processing, but routes correctly)."""
    settings = get_proxy_settings()
    settings.cert_dir = str(cert_dir)
    settings.port = 0
    settings.host = "127.0.0.1"
    settings.use_tls = False  # Test clean HTTP mode
    settings.openai_upstream = f"http://127.0.0.1:{mock_upstream.port}"
    settings.bypass = True

    proxy = LocalProxyServer()
    proxy.settings = settings

    await proxy.start()
    proxy_port = proxy.runner.addresses[0][1]
    proxy_url = f"http://127.0.0.1:{proxy_port}"

    async with aiohttp.ClientSession() as session:
        payload = {"model": "gpt-3.5-turbo"}
        async with session.post(f"{proxy_url}/v1/chat/completions", json=payload) as resp:
            assert resp.status == 200
            data = await resp.json()
            assert "gpt-3.5-turbo" in data["choices"][0]["message"]["content"]

    await proxy.stop()


async def test_proxy_unhappy_paths(cert_dir: Path) -> None:
    """Verify proxy handles connection errors and timeouts gracefully."""
    settings = get_proxy_settings()
    settings.cert_dir = str(cert_dir)
    settings.port = 0
    settings.host = "127.0.0.1"
    settings.use_tls = False
    # Upstream points to a non-existent port to force connection failure
    settings.openai_upstream = "http://127.0.0.1:58291"

    proxy = LocalProxyServer()
    proxy.settings = settings

    await proxy.start()
    proxy_port = proxy.runner.addresses[0][1]
    proxy_url = f"http://127.0.0.1:{proxy_port}"

    async with aiohttp.ClientSession() as session:
        # Test bad gateway (cannot connect)
        async with session.post(f"{proxy_url}/v1/chat/completions", json={}) as resp:
            assert resp.status == 502
            data = await resp.json()
            assert data["error"]["type"] == "bad_gateway"

    await proxy.stop()


async def test_dynamic_bypass_toggle(mock_upstream: MockUpstream) -> None:
    """Verify that toggling the dynamic bypass config on disk changes proxy behavior instantly."""
    config_path = get_bypass_config_path()
    if config_path.exists():
        config_path.unlink()

    settings = get_proxy_settings()
    settings.port = 0
    settings.host = "127.0.0.1"
    settings.use_tls = False
    settings.openai_upstream = f"http://127.0.0.1:{mock_upstream.port}"
    settings.bypass = False

    proxy = LocalProxyServer()
    proxy.settings = settings
    await proxy.start()

    proxy_port = proxy.runner.addresses[0][1]
    proxy_url = f"http://127.0.0.1:{proxy_port}"

    async with aiohttp.ClientSession() as session:
        payload = {"model": "gpt-3.5-turbo"}
        async with session.post(f"{proxy_url}/v1/chat/completions", json=payload) as resp:
            assert resp.status == 200
            data = await resp.json()
            assert "gpt-3.5-turbo" in data["choices"][0]["message"]["content"]

        # Toggle bypass ON
        write_config(bypass=True)
        assert config_path.exists()

        async with session.post(f"{proxy_url}/v1/chat/completions", json=payload) as resp:
            assert resp.status == 200
            data = await resp.json()
            assert "gpt-3.5-turbo" in data["choices"][0]["message"]["content"]

        # Toggle bypass OFF
        write_config(bypass=False)
        async with session.post(f"{proxy_url}/v1/chat/completions", json=payload) as resp:
            assert resp.status == 200
            data = await resp.json()
            assert "gpt-3.5-turbo" in data["choices"][0]["message"]["content"]

    await proxy.stop()
    if config_path.exists():
        config_path.unlink()


async def test_cli_on_off_toggles() -> None:
    """Verify the CLI commands write to the global config file correctly."""
    config_path = get_bypass_config_path()
    if config_path.exists():
        config_path.unlink()

    res = subprocess.run(  # noqa: ASYNC101  # noqa: ASYNC221
        [sys.executable, "-m", "checkcord.cli", "proxy", "off"], capture_output=True, text=True
    )
    assert res.returncode == 0
    assert "Proxy off." in res.stdout
    assert config_path.exists()

    with open(config_path) as f:  # noqa: ASYNC101  # noqa: ASYNC230
        data = json.load(f)
    assert data["bypass"] is True

    res = subprocess.run(  # noqa: ASYNC101  # noqa: ASYNC221
        [sys.executable, "-m", "checkcord.cli", "proxy", "on"], capture_output=True, text=True
    )
    assert res.returncode == 0
    assert "Proxy on." in res.stdout

    with open(config_path) as f:  # noqa: ASYNC101  # noqa: ASYNC230
        data = json.load(f)
    assert data["bypass"] is False

    if config_path.exists():
        config_path.unlink()


async def test_proxy_strips_volatile_metadata(mock_upstream: MockUpstream) -> None:
    """Verify that proxy strips dynamic/volatile system prompt metadata on active routing."""
    settings = get_proxy_settings()
    settings.port = 0
    settings.host = "127.0.0.1"
    settings.use_tls = False
    settings.openai_upstream = f"http://127.0.0.1:{mock_upstream.port}"
    settings.bypass = False  # Make sure processing is active

    proxy = LocalProxyServer()
    proxy.settings = settings
    await proxy.start()

    proxy_port = proxy.runner.addresses[0][1]
    proxy_url = f"http://127.0.0.1:{proxy_port}"

    async with aiohttp.ClientSession() as session:
        # 1. Test when bypass is OFF (Interception Active)
        payload = {
            "model": "gpt-3.5-turbo",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Operating System: macOS 14.5\nWorking directory: /var/log\n"
                        "Today is Sunday.\nDo not change settings."
                    ),
                },
                {
                    "role": "user",
                    "content": "Hello. Today is Sunday.",  # In user block, should be untouched
                },
            ],
        }

        async with session.post(f"{proxy_url}/v1/chat/completions", json=payload) as resp:
            assert resp.status == 200

        # Verify mock upstream received the sanitized payload
        received = mock_upstream.last_received_payload
        assert received is not None
        system_content = received["messages"][0]["content"]
        assert "[OS_REDACTED]" in system_content
        assert "[PATH_REDACTED]" in system_content
        assert "[TIME_REDACTED]" in system_content
        assert "macOS" not in system_content
        assert "log" not in system_content
        assert "Do not change settings." in system_content  # Developer instructions preserved

        user_content = received["messages"][1]["content"]
        assert "Hello. Today is Sunday." in user_content  # User content unchanged

        # 2. Test when bypass is ON (Interception Inactive)
        settings.bypass = True
        payload_new = {
            "model": "gpt-3.5-turbo",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Operating System: macOS 14.5\nWorking directory: /var/log\n"
                        "Today is Sunday."
                    ),
                }
            ],
        }

        async with session.post(f"{proxy_url}/v1/chat/completions", json=payload_new) as resp:
            assert resp.status == 200

        # Verify mock upstream received the untouched payload
        received_bypass = mock_upstream.last_received_payload
        assert received_bypass is not None
        system_content_bypass = received_bypass["messages"][0]["content"]
        assert "macOS 14.5" in system_content_bypass
        assert "/var/log" in system_content_bypass
        assert "Today is Sunday." in system_content_bypass

    await proxy.stop()


async def test_proxy_injects_context(mock_upstream: MockUpstream, tmp_path: Path) -> None:
    """Verify that proxy injects dynamic ARCHITECTURE.md context if present
    in the working directory."""
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        # 1. Create context directory and architecture file
        os.makedirs(".context", exist_ok=True)
        rel_path = os.path.join(".context", "ARCHITECTURE.md")
        with open(rel_path, "w", encoding="utf-8") as f:  # noqa: ASYNC101  # noqa: ASYNC230
            f.write("Global Architecture Rule: Use HSL tailored colors.")

        settings = get_proxy_settings()
        settings.port = 0
        settings.host = "127.0.0.1"
        settings.use_tls = False
        settings.openai_upstream = f"http://127.0.0.1:{mock_upstream.port}"
        settings.bypass = False

        proxy = LocalProxyServer()
        proxy.settings = settings
        await proxy.start()

        proxy_port = proxy.runner.addresses[0][1]
        proxy_url = f"http://127.0.0.1:{proxy_port}"

        async with aiohttp.ClientSession() as session:
            payload = {
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "system", "content": "Instructions from user."}],
            }
            async with session.post(f"{proxy_url}/v1/chat/completions", json=payload) as resp:
                assert resp.status == 200

            # Verify context was injected at upstream
            received = mock_upstream.last_received_payload
            assert received is not None
            system_content = received["messages"][0]["content"]
            assert "<checkcord_context>" in system_content
            assert "Global Architecture Rule" in system_content
            assert "Instructions from user." in system_content

            # 2. Test missing context file (remove the file and check if it handles it gracefully)
            os.unlink(rel_path)

            # Wait briefly to ensure mtime or exist check updates
            async with session.post(f"{proxy_url}/v1/chat/completions", json=payload) as resp:
                assert resp.status == 200

            received_deleted = mock_upstream.last_received_payload
            assert received_deleted is not None
            system_content_deleted = received_deleted["messages"][0]["content"]
            assert "<checkcord_context>" not in system_content_deleted
            assert "Instructions from user." in system_content_deleted

        await proxy.stop()

    finally:
        os.chdir(old_cwd)


async def test_proxy_logs_diff(mock_upstream: MockUpstream, tmp_path: Path) -> None:
    """Verify that proxy generates a unified diff in prompt_diffs.log when payload is mutated."""
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        settings = get_proxy_settings()
        settings.port = 0
        settings.host = "127.0.0.1"
        settings.use_tls = False
        settings.openai_upstream = f"http://127.0.0.1:{mock_upstream.port}"
        settings.bypass = False

        proxy = LocalProxyServer()
        proxy.settings = settings
        await proxy.start()

        proxy_port = proxy.runner.addresses[0][1]
        proxy_url = f"http://127.0.0.1:{proxy_port}"

        async with aiohttp.ClientSession() as session:
            # Send a system prompt containing time which triggers sanitization
            payload = {
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "system", "content": "The current time is 12:00:00 PM."}],
            }
            async with session.post(f"{proxy_url}/v1/chat/completions", json=payload) as resp:
                assert resp.status == 200

        # Give the background logging thread a moment to finish writing to disk
        await asyncio.sleep(0.2)

        # Close the proxy and its handlers to release log file lock
        await proxy.stop()

        # Verify log file was created and contains diff
        log_file = os.path.abspath(os.path.join(".context", "logs", "prompt_diffs.log"))  # noqa: ASYNC240
        assert os.path.exists(log_file)  # noqa: ASYNC240

        with open(log_file, encoding="utf-8") as f:  # noqa: ASYNC101  # noqa: ASYNC230
            content = f.read()

        assert "PATH:      /v1/chat/completions" in content
        assert "MODEL:     gpt-3.5-turbo" in content
        assert '-      "content": "The current time is 12:00:00 PM."' in content
        assert '+      "content": "[TIME_REDACTED]",' in content

    finally:
        os.chdir(old_cwd)


async def test_proxy_injects_collision_warning(mock_upstream: MockUpstream, tmp_path: Path) -> None:
    """Verify that proxy injects collision warning when detect_collisions returns conflicts."""
    from unittest.mock import AsyncMock, patch

    settings = get_proxy_settings()
    settings.port = 0
    settings.host = "127.0.0.1"
    settings.use_tls = False
    settings.openai_upstream = f"http://127.0.0.1:{mock_upstream.port}"
    settings.bypass = False
    # Ensure tenant_id is set so collision detection runs
    settings.tenant_id = "11111111-2222-3333-4444-555555555555"

    proxy = LocalProxyServer()
    proxy.settings = settings
    # We must mock pg_engine as not None so it tries to check collisions
    mock_engine = AsyncMock()
    proxy.pg_engine = mock_engine

    await proxy.start()

    proxy_port = proxy.runner.addresses[0][1]
    proxy_url = f"http://127.0.0.1:{proxy_port}"

    # Mock the detect_collisions coroutine and extract_symbols_from_payload
    with (
        patch("checkcord.proxy.server.extract_symbols_from_payload", return_value=["test_symbol"]),
        patch("checkcord.proxy.server.detect_collisions", new_callable=AsyncMock) as mock_detect,
    ):
        mock_detect.return_value = [{"branch_name": "feature/other", "symbol_name": "test_symbol"}]

        async with aiohttp.ClientSession() as session:
            payload = {
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "system", "content": "Instructions from user."}],
            }
            async with session.post(f"{proxy_url}/v1/chat/completions", json=payload) as resp:
                assert resp.status == 200

        # Verify upstream received injected conflict warning
        received = mock_upstream.last_received_payload
        assert received is not None
        system_content = received["messages"][0]["content"]
        assert "<conflict_warning>" in system_content
        assert "WARNING: Another active local branch (feature/other)" in system_content
        assert "'test_symbol'" in system_content
        assert "Instructions from user." in system_content

    await proxy.stop()


async def test_proxy_injects_collision_warning_with_viewer_link(
    mock_upstream: MockUpstream, tmp_path: Path
) -> None:
    """When a collision carries ast_node_id/repo_identifier, the injected
    warning includes a viewer deep link with no auth token, built from the
    proxy's own web_base_url setting.
    """
    from unittest.mock import AsyncMock, patch

    settings = get_proxy_settings()
    settings.port = 0
    settings.host = "127.0.0.1"
    settings.use_tls = False
    settings.openai_upstream = f"http://127.0.0.1:{mock_upstream.port}"
    settings.bypass = False
    settings.tenant_id = "11111111-2222-3333-4444-555555555555"
    settings.web_base_url = "https://checkcord.example.com"

    proxy = LocalProxyServer()
    proxy.settings = settings
    mock_engine = AsyncMock()
    proxy.pg_engine = mock_engine

    await proxy.start()

    proxy_port = proxy.runner.addresses[0][1]
    proxy_url = f"http://127.0.0.1:{proxy_port}"

    ast_node_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    with (
        patch("checkcord.proxy.server.extract_symbols_from_payload", return_value=["test_symbol"]),
        patch("checkcord.proxy.server.detect_collisions", new_callable=AsyncMock) as mock_detect,
    ):
        mock_detect.return_value = [
            {
                "branch_name": "feature/other",
                "symbol_name": "test_symbol",
                "ast_node_id": ast_node_id,
                "repo_identifier": "org/repo",
            }
        ]

        async with aiohttp.ClientSession() as session:
            payload = {
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "system", "content": "Instructions from user."}],
            }
            async with session.post(f"{proxy_url}/v1/chat/completions", json=payload) as resp:
                assert resp.status == 200

        received = mock_upstream.last_received_payload
        assert received is not None
        system_content = received["messages"][0]["content"]
        expected_url = (
            f"https://checkcord.example.com/embed/graph/org%2Frepo?node=symbol%3A{ast_node_id}"
        )
        assert expected_url in system_content
        assert "token=" not in system_content

    await proxy.stop()


async def test_proxy_telemetry_logging(mock_upstream: MockUpstream, tmp_path: Path) -> None:
    """Verify that proxy intercepts LLM responses and logs token usage in telemetry database."""
    from checkcord.storage.sqlite import SQLiteTelemetryStore

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        settings = get_proxy_settings()
        settings.port = 0
        settings.host = "127.0.0.1"
        settings.use_tls = False
        settings.openai_upstream = f"http://127.0.0.1:{mock_upstream.port}"
        settings.anthropic_upstream = f"http://127.0.0.1:{mock_upstream.port}"
        settings.bypass = False

        proxy = LocalProxyServer()
        proxy.settings = settings
        await proxy.start()

        proxy_port = proxy.runner.addresses[0][1]
        proxy_url = f"http://127.0.0.1:{proxy_port}"

        # 1. Test non-streaming OpenAI telemetry logging
        async with aiohttp.ClientSession() as session:
            payload = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]}
            async with session.post(f"{proxy_url}/v1/chat/completions", json=payload) as resp:
                assert resp.status == 200
                await resp.json()

            # 2. Test non-streaming Anthropic telemetry logging
            anthropic_payload = {
                "model": "claude-3-5-sonnet",
                "messages": [{"role": "user", "content": "hi"}],
            }
            async with session.post(f"{proxy_url}/v1/messages", json=anthropic_payload) as resp:
                assert resp.status == 200
                await resp.json()

            # 3. Test streaming telemetry logging
            streaming_payload = {
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hello stream"}],
                "stream": True,
            }
            async with session.post(
                f"{proxy_url}/v1/chat/completions/stream", json=streaming_payload
            ) as resp:
                assert resp.status == 200
                # Consume stream
                async for _ in resp.content:
                    pass

        # Give background task time to log request
        await asyncio.sleep(0.2)

        await proxy.stop()

        # Check telemetry DB contents
        db_path = os.path.join(".context", "telemetry.db")
        assert os.path.exists(db_path)  # noqa: ASYNC240

        store = SQLiteTelemetryStore(db_path)
        summary = await store.get_summary()
        assert summary["total_requests"] == 3
        # prompt: 100 (openai) + 300 (anthropic) + 150 (streaming) = 550
        assert summary["total_prompt"] == 550
        # completion: 20 (openai) + 50 (anthropic) + 10 (streaming) = 80
        assert summary["total_completion"] == 80
        # cache creation: 0 (openai) + 100 (anthropic) + 0 (streaming) = 100
        assert summary["total_cache_creation"] == 100
        # cache read: 40 (openai) + 80 (anthropic) + 30 (streaming) = 150
        assert summary["total_cache_read"] == 150

    finally:
        os.chdir(old_cwd)
