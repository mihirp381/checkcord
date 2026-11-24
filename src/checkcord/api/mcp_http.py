"""The MCP tools over streamable HTTP at /mcp, authenticated by API key."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from checkcord.api import rate_limit
from checkcord.api.deps import (
    Principal,
    bearer_token,
    get_db_engine,
    record_auth_failure,
    resolve_principal,
)
from checkcord.core.config import get_settings
from checkcord.pipeline.embedder import EmbeddingClient
from checkcord.pipeline.embeddings_factory import build_embeddings_provider
from checkcord.services import mcp_tools
from checkcord.storage.audit import MCP

_SCOPE_KEY = "checkcord.mcp_caller"


@dataclass(frozen=True)
class _Caller:
    engine: AsyncEngine
    principal: Principal


_embedding_client: EmbeddingClient | None = None


def _embedder() -> EmbeddingClient | None:
    global _embedding_client
    if _embedding_client is None:
        provider = build_embeddings_provider(get_settings())
        if provider is None:
            return None
        _embedding_client = EmbeddingClient(
            provider, purpose="search", dimension=get_settings().embedding_dimension
        )
    return _embedding_client


def _caller(ctx: Context[Any, Any, Any]) -> _Caller:
    request = ctx.request_context.request
    caller = request.scope.get(_SCOPE_KEY) if isinstance(request, Request) else None
    if not isinstance(caller, _Caller):
        raise PermissionError("unauthenticated MCP request")
    return caller


def build_mcp_server() -> FastMCP:
    server = FastMCP(
        "Checkcord",
        stateless_http=True,
        json_response=True,
        # Auth is a Bearer header, which a rebinding browser cannot attach.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @server.tool(description=mcp_tools.DEPENDENCY_FLOW)
    async def get_dependency_flow(file_path: str, ctx: Context[Any, Any, Any]) -> str:
        c = _caller(ctx)
        return await mcp_tools.dependency_flow(
            c.engine, c.principal.tenant_id, c.principal.as_actor(MCP), file_path
        )

    @server.tool(description=mcp_tools.ACTIVE_DECISIONS)
    async def view_active_decisions(
        ctx: Context[Any, Any, Any],
        repo_identifier: str | None = None,
        team_identifier: str | None = None,
        path: str | None = None,
        limit: int = mcp_tools.DEFAULT_DECISIONS,
    ) -> str:
        c = _caller(ctx)
        return await mcp_tools.active_decisions(
            c.engine,
            c.principal.tenant_id,
            c.principal.as_actor(MCP),
            repo_identifier=repo_identifier,
            team_identifier=team_identifier,
            path=path,
            limit=limit,
        )

    @server.tool(description=mcp_tools.CONCEPTUAL_SEARCH)
    async def search_conceptual_context(
        query: str,
        ctx: Context[Any, Any, Any],
        repo_identifier: str | None = None,
        team_identifier: str | None = None,
        path: str | None = None,
        limit: int = mcp_tools.DEFAULT_RESULTS,
    ) -> str:
        c = _caller(ctx)
        return await mcp_tools.conceptual_search(
            c.engine,
            c.principal.tenant_id,
            c.principal.as_actor(MCP),
            _embedder,
            query,
            repo_identifier=repo_identifier,
            team_identifier=team_identifier,
            path=path,
            limit=limit,
        )

    return server


@asynccontextmanager
async def mcp_lifespan(app: FastAPI) -> AsyncIterator[None]:
    # A session manager runs once, so each app start gets a fresh server.
    server = build_mcp_server()
    server.streamable_http_app()
    app.state.mcp_session_manager = server.session_manager
    async with server.session_manager.run():
        yield


class MCPEndpoint:
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        token = bearer_token(request.headers.get("authorization"))
        engine = await get_db_engine(request)
        principal = await resolve_principal(token, engine) if token else None
        if principal is None:
            if token:
                await record_auth_failure(request, token, engine)
            response = JSONResponse(
                {"detail": "Unauthorized: a valid API key is required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        wait = await rate_limit.principal_wait(request, principal)
        if wait is not None:
            await rate_limit.refusal_response(rate_limit.PRINCIPAL, wait)(scope, receive, send)
            return
        scope[_SCOPE_KEY] = _Caller(engine=engine, principal=principal)
        await request.app.state.mcp_session_manager.handle_request(scope, receive, send)
