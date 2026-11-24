"""`checkcord mcp`: the MCP tools over stdio, forwarded to a Checkcord server's /mcp with a key."""

import json
import logging
import sys
from typing import Any

import httpx
import structlog
from mcp.server.fastmcp import FastMCP

from checkcord.proxy.config import get_proxy_settings
from checkcord.services.mcp_tools import (
    ACTIVE_DECISIONS,
    CONCEPTUAL_SEARCH,
    DEFAULT_DECISIONS,
    DEFAULT_RESULTS,
    DEPENDENCY_FLOW,
    normalize_mcp_path,
)

logger = structlog.get_logger(__name__)

mcp = FastMCP("Checkcord")


def _remote() -> tuple[str, str] | None:
    settings = get_proxy_settings()
    if settings.api_url and settings.api_key:
        return settings.api_url, settings.api_key.get_secret_value()
    return None


def remote_mcp_url(api_url: str) -> str:
    base = api_url.rstrip("/")
    return base if base.endswith("/mcp") else f"{base}/mcp"


def _remote_http_client(api_key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx.Timeout(30.0, read=300.0),
    )


async def call_remote_tool(name: str, arguments: dict[str, Any]) -> str:
    remote = _remote()
    if remote is None:
        return json.dumps(
            {
                "error": (
                    "checkcord mcp needs CHECKCORD_PROXY_API_URL and CHECKCORD_PROXY_API_KEY: "
                    "your Checkcord server and your own key."
                )
            }
        )
    api_url, api_key = remote
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.types import TextContent

    args = {k: v for k, v in arguments.items() if v is not None}
    try:
        async with (
            _remote_http_client(api_key) as http,
            streamable_http_client(remote_mcp_url(api_url), http_client=http) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(name, args)
    except Exception as e:
        logger.error("remote_mcp_call_failed", tool=name, error=str(e))
        return json.dumps({"error": f"Checkcord API call failed: {e}"})

    text = "".join(c.text for c in result.content if isinstance(c, TextContent))
    return json.dumps({"error": text}) if result.isError else text


@mcp.tool(description=DEPENDENCY_FLOW)
async def get_dependency_flow(file_path: str) -> str:
    return await call_remote_tool(
        "get_dependency_flow", {"file_path": normalize_mcp_path(file_path)}
    )


@mcp.tool(description=ACTIVE_DECISIONS)
async def view_active_decisions(
    repo_identifier: str | None = None,
    team_identifier: str | None = None,
    path: str | None = None,
    limit: int = DEFAULT_DECISIONS,
) -> str:
    return await call_remote_tool(
        "view_active_decisions",
        {
            "repo_identifier": repo_identifier,
            "team_identifier": team_identifier,
            "path": path,
            "limit": limit,
        },
    )


@mcp.tool(description=CONCEPTUAL_SEARCH)
async def search_conceptual_context(
    query: str,
    repo_identifier: str | None = None,
    team_identifier: str | None = None,
    path: str | None = None,
    limit: int = DEFAULT_RESULTS,
) -> str:
    return await call_remote_tool(
        "search_conceptual_context",
        {
            "query": query,
            "repo_identifier": repo_identifier,
            "team_identifier": team_identifier,
            "path": path,
            "limit": limit,
        },
    )


def main() -> None:
    # Ensure logging is strictly routed to stderr to avoid stdout corruption
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
    )

    # This runs the JSON-RPC server over stdio
    mcp.run()


if __name__ == "__main__":
    main()
