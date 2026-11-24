import asyncio
import json
import os
import ssl
from pathlib import Path
from uuid import UUID

import aiohttp
import structlog
from aiohttp import web

from checkcord.core.viewer_links import build_node_url
from checkcord.proxy.audit import AuditLogger
from checkcord.proxy.cert_manager import generate_certs
from checkcord.proxy.collision import detect_collisions, extract_symbols_from_payload
from checkcord.proxy.config import get_proxy_settings, read_dynamic_bypass
from checkcord.proxy.context import DynamicContextCache
from checkcord.proxy.injector import inject_payload
from checkcord.proxy.ledger import GitWatcher
from checkcord.proxy.sanitizer import sanitize_payload
from checkcord.storage.postgres import create_checkcord_engine
from checkcord.storage.sqlite import SQLiteTelemetryStore

logger = structlog.get_logger(__name__)

CLIENT_SESSION_KEY: web.AppKey[aiohttp.ClientSession] = web.AppKey(
    "client_session", aiohttp.ClientSession
)


class LocalProxyServer:
    """The local HTTP and HTTPS proxy."""

    def __init__(self) -> None:
        self.settings = get_proxy_settings()
        self.app = web.Application()
        self._setup_routes()
        self.client_session: aiohttp.ClientSession | None = None
        self.runner: web.AppRunner | None = None
        self.context_cache = DynamicContextCache()
        self.audit_logger = AuditLogger()
        self.git_watcher = GitWatcher(workspace_path=".", db_path=".context/ledger.db")
        self.telemetry_store = SQLiteTelemetryStore(".context/telemetry.db")
        self.pg_engine = None
        if self.settings.database_url:
            self.pg_engine = create_checkcord_engine(self.settings.database_url)

        # Register lifecycle handlers
        self.app.on_startup.append(self._on_startup)
        self.app.on_cleanup.append(self._on_cleanup)

    def _setup_routes(self) -> None:
        """Register catch-all route to intercept all incoming requests."""
        self.app.router.add_route("*", "/{tail:.*}", self.handle_proxy)

    async def _on_startup(self, app: web.Application) -> None:
        """Initialize shared ClientSession with auto_decompress=False to prevent
        header/body mismatch."""
        # auto_decompress=False is critical to forward raw compressed streams without corruption
        self.client_session = aiohttp.ClientSession(auto_decompress=False)
        app[CLIENT_SESSION_KEY] = self.client_session
        logger.info("proxy_client_session_initialized")
        await self.git_watcher.start()
        await self.telemetry_store.init_db()

    async def _on_cleanup(self, app: web.Application) -> None:
        """Close client session and clean up resource connections."""
        await self.git_watcher.stop()
        self.audit_logger.close()
        if self.pg_engine:
            await self.pg_engine.dispose()
            logger.info("proxy_postgres_engine_disposed")
        if self.client_session:
            await self.client_session.close()
            logger.info("proxy_client_session_closed")

    async def handle_proxy(self, request: web.Request) -> web.StreamResponse:
        """Intercepts, logs, and forwards requests to the correct upstream provider."""
        path = request.path
        method = request.method

        # Check if bypass is active
        is_bypass = (
            self.settings.bypass
            or os.environ.get("CHECKCORD_BYPASS") == "true"
            or read_dynamic_bypass()
        )

        # Determine upstream URL based on path
        # Anthropic endpoint checks
        if "messages" in path or "complete" in path or "anthropic" in path:
            upstream_base = self.settings.anthropic_upstream
            provider = "anthropic"
        else:
            upstream_base = self.settings.openai_upstream
            provider = "openai"

        upstream_url = f"{upstream_base.rstrip('/')}{path}"

        logger.info(
            "proxy_intercepted_request",
            method=method,
            path=path,
            upstream=upstream_url,
            bypass=is_bypass,
        )

        # Extract headers and sanitize for forwarding
        headers = dict(request.headers)
        headers.pop("Host", None)
        headers.pop("Content-Length", None)

        # Read the client request body
        body = await request.read()

        is_stream = False
        model = "unknown"

        # If not bypassed, we log details of the payload for debugging/auditing and sanitize it
        if not is_bypass:
            logger.debug(
                "proxy_payload_audit",
                path=path,
                payload_len=len(body),
                content_type=headers.get("Content-Type"),
            )
            content_type = headers.get("Content-Type", "")
            if "application/json" in content_type and body:
                try:
                    payload_dict = json.loads(body)
                    is_stream = payload_dict.get("stream", False)
                    model = payload_dict.get("model", "unknown")
                    sanitized_dict = sanitize_payload(payload_dict)

                    # Extract symbols and run collision detection
                    collision_warning = None
                    if self.pg_engine and self.settings.tenant_id:
                        try:
                            symbols = extract_symbols_from_payload(sanitized_dict)
                            current_branch = self.git_watcher.current_branch
                            # Detect collisions (with 150ms timeout)
                            if symbols:
                                try:
                                    tenant_uuid = UUID(self.settings.tenant_id)
                                    collisions = await asyncio.wait_for(
                                        detect_collisions(
                                            symbols=symbols,
                                            current_branch=current_branch,
                                            tenant_id=tenant_uuid,
                                            pg_engine=self.pg_engine,
                                            ledger_db_path=self.git_watcher.db_path,
                                        ),
                                        timeout=0.150,
                                    )
                                    if collisions:
                                        warnings = []
                                        for c in collisions:
                                            warning_line = (
                                                f"WARNING: Another active local "
                                                f"branch ({c['branch_name']}) "
                                                f"is modifying the symbol '{c['symbol_name']}'."
                                            )
                                            # Link to the symbol in the graph
                                            # viewer. No auth token: this text
                                            # goes to an LLM provider.
                                            if c.get("ast_node_id") and c.get("repo_identifier"):
                                                node_url = build_node_url(
                                                    self.settings.web_base_url,
                                                    c["repo_identifier"],
                                                    f"symbol:{c['ast_node_id']}",
                                                )
                                                warning_line += (
                                                    f" View linked architectural "
                                                    f"decisions: {node_url}"
                                                )
                                            warnings.append(warning_line)
                                        collision_warning = (
                                            "<conflict_warning>\n"
                                            + "\n".join(warnings)
                                            + "\n</conflict_warning>"
                                        )
                                        logger.info("collision_detected", count=len(collisions))
                                except TimeoutError:
                                    logger.warning("collision_detection_timeout_failing_open")
                                except ValueError as ve:
                                    logger.error("invalid_tenant_uuid_format", error=str(ve))
                        except Exception as ce:
                            logger.error("collision_detection_failed_failing_open", error=str(ce))

                    # Fetch and inject context if it exists
                    context_str = await self.context_cache.get_context_string()
                    is_anthropic = "messages" in path or "complete" in path or "anthropic" in path
                    sanitized_dict = inject_payload(
                        sanitized_dict,
                        context_str,
                        collision_warning=collision_warning,
                        is_anthropic=is_anthropic,
                    )

                    if sanitized_dict != payload_dict:
                        asyncio.create_task(
                            asyncio.to_thread(
                                self.audit_logger.log_diff,
                                path,
                                payload_dict,
                                sanitized_dict,
                            )
                        )

                    body = json.dumps(sanitized_dict).encode("utf-8")
                except json.JSONDecodeError as e:
                    logger.warning("proxy_json_decode_failed_failing_open", error=str(e))
                except Exception as e:
                    logger.error(
                        "proxy_sanitization_failed_failing_open", error=str(e), exc_info=True
                    )

        # Forward the request to the upstream LLM API
        session = request.app[CLIENT_SESSION_KEY]

        try:
            # We use short timeouts for client connections to prevent hanging
            # but allow long read timeouts for streaming responses
            timeout = aiohttp.ClientTimeout(connect=10.0, total=None, sock_read=None)

            async with session.request(
                method=method,
                url=upstream_url,
                headers=headers,
                data=body,
                params=request.query,
                allow_redirects=False,
                timeout=timeout,
            ) as resp:
                # Prepare response stream to client
                resp_headers = dict(resp.headers)
                # Let aiohttp web server handle the Transfer-Encoding header
                resp_headers.pop("Transfer-Encoding", None)
                resp_headers.pop("Content-Length", None)

                client_resp = web.StreamResponse(
                    status=resp.status, reason=resp.reason, headers=resp_headers
                )

                await client_resp.prepare(request)

                try:
                    # Stream the response chunks directly to client to prevent buffering latency
                    # Telemetry integration
                    import zlib

                    from checkcord.proxy.telemetry import (
                        SSEParser,
                        UsageAccumulator,
                        extract_usage_from_body,
                    )

                    accumulator = UsageAccumulator(provider)
                    parser = SSEParser()
                    response_bytes = b""

                    content_encoding = resp.headers.get("Content-Encoding", "").lower()
                    decompressor = None
                    if "gzip" in content_encoding:
                        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    elif "deflate" in content_encoding:
                        decompressor = zlib.decompressobj(-zlib.MAX_WBITS)

                    async for chunk in resp.content.iter_any():
                        await client_resp.write(chunk)

                        if not is_bypass and resp.status == 200:
                            parser_chunk = chunk
                            if decompressor:
                                try:
                                    parser_chunk = decompressor.decompress(chunk)
                                except Exception as e:
                                    logger.warning("telemetry_decompression_failed", error=str(e))
                                    parser_chunk = b""

                            if is_stream:
                                try:
                                    events = parser.feed(parser_chunk)
                                    for event in events:
                                        accumulator.feed_event(event)
                                except Exception as e:
                                    logger.warning(
                                        "telemetry_streaming_chunk_parse_failed", error=str(e)
                                    )
                            else:
                                response_bytes += parser_chunk

                    if decompressor:
                        try:
                            parser_chunk = decompressor.flush()
                            if parser_chunk:
                                if is_stream:
                                    events = parser.feed(parser_chunk)
                                    for event in events:
                                        accumulator.feed_event(event)
                                else:
                                    response_bytes += parser_chunk
                        except Exception as e:
                            logger.warning("telemetry_decompression_flush_failed", error=str(e))

                    await client_resp.write_eof()

                    # Save telemetry logs after completion
                    if not is_bypass and resp.status == 200:
                        usage = None
                        if is_stream:
                            usage = accumulator.get_dict()
                        else:
                            try:
                                resp_body = json.loads(
                                    response_bytes.decode("utf-8", errors="ignore")
                                )
                                usage = extract_usage_from_body(resp_body, provider)
                            except Exception as e:
                                logger.warning(
                                    "telemetry_non_streaming_body_parse_failed", error=str(e)
                                )

                        if usage and (usage["prompt_tokens"] > 0 or usage["completion_tokens"] > 0):
                            asyncio.create_task(
                                self.telemetry_store.log_request(
                                    provider=provider,
                                    model=model,
                                    prompt_tokens=usage["prompt_tokens"],
                                    completion_tokens=usage["completion_tokens"],
                                    cache_creation_input_tokens=usage[
                                        "cache_creation_input_tokens"
                                    ],
                                    cache_read_input_tokens=usage["cache_read_input_tokens"],
                                )
                            )
                except (asyncio.CancelledError, ConnectionResetError):
                    logger.debug("proxy_client_disconnected_early", path=path)

                return client_resp

        except TimeoutError:
            logger.error("proxy_upstream_timeout", url=upstream_url)
            return web.json_response(
                {
                    "error": {
                        "message": "Upstream request timed out.",
                        "type": "gateway_timeout",
                        "code": 504,
                    }
                },
                status=504,
            )
        except Exception as e:
            logger.error("proxy_forward_failed", url=upstream_url, error=str(e), exc_info=True)
            return web.json_response(
                {
                    "error": {
                        "message": f"Proxy forwarding error: {str(e)}",
                        "type": "bad_gateway",
                        "code": 502,
                    }
                },
                status=502,
            )

    async def start(self) -> None:
        """Starts the proxy server runner using TLS configuration if enabled."""
        ssl_ctx = None
        if self.settings.use_tls:
            cert_dir = Path(self.settings.cert_dir)
            # Ensure certs are generated
            generate_certs(cert_dir)

            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            try:
                ssl_ctx.load_cert_chain(
                    certfile=self.settings.cert_path, keyfile=self.settings.key_path
                )
                logger.info("loaded_ssl_certificates", cert=str(self.settings.cert_path))
            except Exception as e:
                logger.error(
                    "failed_to_load_ssl_certificates",
                    cert=str(self.settings.cert_path),
                    error=str(e),
                )
                logger.info("falling_back_to_http")
                ssl_ctx = None

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()

        site = web.TCPSite(
            self.runner, host=self.settings.host, port=self.settings.port, ssl_context=ssl_ctx
        )

        await site.start()
        proto = "https" if ssl_ctx else "http"
        logger.info(
            "proxy_server_started", url=f"{proto}://{self.settings.host}:{self.settings.port}"
        )

    async def stop(self) -> None:
        """Stops the proxy server runner and releases bound ports."""
        if self.runner:
            await self.runner.cleanup()
            logger.info("proxy_server_stopped")
