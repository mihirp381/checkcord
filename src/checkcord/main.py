from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from structlog.contextvars import bind_contextvars, clear_contextvars

from checkcord.connectors import ListenerContext, listeners
from checkcord.core.config import get_settings
from checkcord.core.lifecycle import app_lifecycle
from checkcord.core.logging import setup_logging
from checkcord.core.startup import (
    enforce_database_role,
    enforce_embedding_column,
    validate_security_settings,
)
from checkcord.core.version import checkcord_version
from checkcord.pipeline.scheduler import create_scheduler

logger = structlog.get_logger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Binds a request id and logs each request."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        clear_contextvars()
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )

        logger.info("request_started")
        start_time = time.perf_counter()

        try:
            response: Response = await call_next(request)
            duration = time.perf_counter() - start_time
            logger.info(
                "request_finished",
                status_code=response.status_code,
                duration_ms=round(duration * 1000, 2),
            )
            response.headers["X-Request-ID"] = request_id
            return response
        except Exception as e:
            duration = time.perf_counter() - start_time
            logger.error(
                "request_failed",
                error=str(e),
                duration_ms=round(duration * 1000, 2),
                exc_info=True,
            )
            raise
        finally:
            clear_contextvars()


HEALTH_PATHS = frozenset({"/healthz", "/readyz"})
SIGN_IN_PATHS = frozenset({"/api/v1/auth/login", "/api/v1/auth/callback"})


def _route_template(request: Request) -> str:
    """The matched route's path template, never the path itself, so no ids reach a label."""
    route = request.scope.get("route")
    endpoint = request.scope.get("endpoint")
    if route is None and endpoint is not None:
        route = next(
            (r for r in request.app.routes if getattr(r, "endpoint", None) is endpoint), None
        )
    return getattr(route, "path", None) or "unmatched"


class RequestLimitsMiddleware(BaseHTTPMiddleware):
    """Per-address limits on failed authentication and sign-in, and request metrics."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        from checkcord.api import rate_limit
        from checkcord.core import metrics

        path = request.url.path
        if path in HEALTH_PATHS:
            response: Response = await call_next(request)
            return response
        start = time.perf_counter()
        wait = await rate_limit.failing_source(request)
        if wait is None and path in SIGN_IN_PATHS:
            wait = await rate_limit.sign_in_attempt(request)
            limit = rate_limit.SIGN_IN
        else:
            limit = rate_limit.AUTH_FAILURES
        status = 500
        try:
            if wait is not None:
                response = rate_limit.refusal_response(limit, wait)
            else:
                response = await call_next(request)
                if response.status_code == 401:
                    await rate_limit.count_failure(request)
            status = response.status_code
            return response
        finally:
            route = _route_template(request)
            metrics.HTTP_REQUESTS.labels(route, str(status)).inc()
            metrics.HTTP_DURATION.labels(route).observe(time.perf_counter() - start)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Context manager for the FastAPI app lifecycle."""
    settings = get_settings()

    # Configure logging
    setup_logging(
        log_level=settings.log_level,
        json_format=(settings.environment != "development"),
    )

    validate_security_settings(settings)
    logger.info("api_starting")

    async with app_lifecycle() as context:
        if context.db_engine is not None:
            await enforce_database_role(settings, context.db_engine)
            await enforce_embedding_column(settings, context.db_engine)
        app.state.ctx = context

        listener_tasks: list[asyncio.Task[None]] = []
        scheduler = None
        pr_evaluator_task = None
        pr_status_reporter_task = None
        if context.redis_client:
            try:
                from checkcord.workers.pr_evaluator import PRWorker

                pr_worker = PRWorker(context.redis_client, context.db_engine)
                pr_evaluator_task = asyncio.create_task(pr_worker.start())
                logger.info("pr_evaluator_worker_started")
            except Exception as e:
                logger.error("failed_to_start_pr_evaluator_worker", error=str(e))

            try:
                from checkcord.workers.pr_status_reporter import PRStatusWorker

                pr_status_worker = PRStatusWorker(context.redis_client, context.db_engine)
                pr_status_reporter_task = asyncio.create_task(pr_status_worker.start())
                logger.info("pr_status_reporter_worker_started")
            except Exception as e:
                logger.error("failed_to_start_pr_status_reporter_worker", error=str(e))

        if context.db_engine:
            try:
                scheduler = create_scheduler(context.db_engine, context.redis_client)
                scheduler.start()
                logger.info("scheduler.started")
            except Exception as e:
                logger.error("failed_to_start_scheduler", error=str(e))
        if context.db_engine and context.redis_client:
            from checkcord.bus.redis_streams import RedisStreamsEventBus

            listener_tasks = await listeners.start(
                "api",
                ListenerContext(
                    settings, context.db_engine, RedisStreamsEventBus(context.redis_client)
                ),
            )

        try:
            from checkcord.api.mcp_http import mcp_lifespan

            async with mcp_lifespan(app):
                yield
        finally:
            if pr_evaluator_task:
                logger.info("stopping_pr_evaluator_worker")
                pr_evaluator_task.cancel()
                try:
                    await pr_evaluator_task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error("error_stopping_pr_evaluator_worker", error=str(e))

            if pr_status_reporter_task:
                logger.info("stopping_pr_status_reporter_worker")
                pr_status_reporter_task.cancel()
                try:
                    await pr_status_reporter_task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error("error_stopping_pr_status_reporter_worker", error=str(e))

            oidc = getattr(app.state, "oidc", None)
            if oidc is not None:
                app.state.oidc = None
                await oidc.aclose()
            sign_in_redis = getattr(app.state, "sign_in_redis", None)
            if sign_in_redis is not None:
                app.state.sign_in_redis = None
                await sign_in_redis.aclose()

            if context.db_engine is not None:
                from checkcord.llm.usage import flush as flush_usage

                await flush_usage(context.db_engine)

            if scheduler:
                logger.info("stopping_scheduler")
                try:
                    scheduler.shutdown()
                    logger.info("scheduler.stopped")
                except Exception as e:
                    logger.error("error_stopping_scheduler", error=str(e))

            await listeners.stop(listener_tasks)

    logger.info("api_stopped")


def create_app() -> FastAPI:
    """Create and configure a FastAPI application instance."""
    settings = get_settings()
    docs = settings.docs_enabled
    app = FastAPI(
        title="Checkcord API",
        version=checkcord_version(),
        description="Checkcord: shared engineering context for coding agents and reviewers. "
        "Authenticate with `Authorization: Bearer <API key>`, or sign in through "
        "`/api/v1/auth/login` and send the session cookie with the `X-CSRF-Token` header "
        "on requests that change state.",
        lifespan=lifespan,
        openapi_url="/openapi.json" if docs else None,
        docs_url="/docs" if docs else None,
        redoc_url=None,
    )

    app.add_middleware(RequestLimitsMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Custom Middleware: Request Logging & Context Binding
    app.add_middleware(RequestLoggingMiddleware)

    from checkcord.api.admin_access_router import router as admin_access_router
    from checkcord.api.admin_ops_router import router as admin_ops_router
    from checkcord.api.admin_router import router as admin_router
    from checkcord.api.agent_ledger_router import router as agent_ledger_router
    from checkcord.api.agent_router import router as agent_router
    from checkcord.api.auth_router import router as auth_router
    from checkcord.api.graph_router import records_router
    from checkcord.api.graph_router import router as graph_router
    from checkcord.scim.protocol import SCIMException, SCIMResponse
    from checkcord.scim.router import router as scim_router

    @app.exception_handler(SCIMException)
    async def scim_exception_handler(request: Request, exc: SCIMException) -> JSONResponse:
        return SCIMResponse(exc.body(), exc.status_code)

    app.include_router(scim_router, tags=["scim"])

    from checkcord import connectors

    for connector in connectors.connectors():
        for router, tag in connector.routers:
            app.include_router(router, tags=[tag] if tag else None)
    app.include_router(agent_router, tags=["agents"])
    app.include_router(agent_ledger_router, tags=["agents"])
    app.include_router(graph_router)
    app.include_router(records_router)
    app.include_router(admin_router)
    app.include_router(admin_access_router)
    app.include_router(admin_ops_router)
    app.include_router(auth_router)

    from starlette.routing import Route

    from checkcord.api.mcp_http import MCPEndpoint

    app.router.routes.append(
        Route("/mcp", MCPEndpoint(), methods=["GET", "POST", "DELETE"], include_in_schema=False)
    )

    @app.get("/healthz", tags=["health"], summary="Liveness")
    async def healthz() -> dict[str, str]:
        """The process answers. It does not check Postgres or Redis; `/readyz` does."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"], summary="Readiness")
    async def readyz(request: Request) -> JSONResponse:
        """200 when Postgres answers at the migration head and Redis answers, else 503."""
        from checkcord.services import readiness

        ctx = getattr(request.app.state, "ctx", None)
        results = await readiness.checks(
            getattr(ctx, "db_engine", None), getattr(ctx, "redis_client", None)
        )
        ok = readiness.ready(results)
        return JSONResponse(
            {"status": "ready" if ok else "not ready", "checks": results},
            status_code=200 if ok else 503,
        )

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics(request: Request) -> Response:
        from checkcord.core import metrics

        current = get_settings()
        if not current.metrics_enabled:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if not metrics.token_ok(request.headers.get("authorization"), current.metrics_token):
            return JSONResponse(
                {"detail": "Unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
        ctx = getattr(request.app.state, "ctx", None)
        await metrics.collect(
            getattr(ctx, "db_engine", None),
            getattr(ctx, "redis_client", None),
            current.audience_refresh_grace_hours,
        )
        body, content_type = metrics.exposition()
        return Response(body, media_type=content_type)

    return app


app = create_app()
