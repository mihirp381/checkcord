"""Pipeline consumers: the process `checkcord worker` runs."""

import asyncio
import signal
from pathlib import Path
from typing import Any

import structlog
from aiohttp import web
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.bus.stream_trim import trim_acknowledged
from checkcord.connectors import ListenerContext, listeners
from checkcord.core import metrics
from checkcord.core.config import get_settings
from checkcord.core.logging import setup_logging
from checkcord.core.startup import (
    StartupConfigError,
    enforce_database_role,
    enforce_embedding_column,
    validate_security_settings,
    validate_worker_settings,
)
from checkcord.llm import usage
from checkcord.llm.client import LLMClient
from checkcord.pipeline import redaction
from checkcord.pipeline.ast_linker import ASTLinkerWorker
from checkcord.pipeline.chat_extractor import ChatExtractor
from checkcord.pipeline.chat_router import ChatRouter
from checkcord.pipeline.clustering import ClusteringWorker
from checkcord.pipeline.consumer import PipelineConsumer
from checkcord.pipeline.embeddings_factory import build_embeddings_provider
from checkcord.pipeline.enricher import PipelineEnricher
from checkcord.pipeline.linkage import LinkageWorker
from checkcord.pipeline.scrubber import PipelineScrubber
from checkcord.pipeline.skill_synthesizer import SkillSynthesisWorker
from checkcord.services import readiness
from checkcord.services.audiences import AUDIENCE_TOPIC
from checkcord.storage.postgres import create_checkcord_engine
from checkcord.storage.scheduled_jobs import interval_gap, run_once
from checkcord.workers.ast_writer_worker import ASTWriterWorker
from checkcord.workers.context_state_compiler import ContextStateCompilerWorker
from checkcord.workers.repo_backfill import RepoBackfillWorker
from checkcord.workers.transcript_worker import TranscriptExtractionWorker

logger = structlog.get_logger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 15


async def _heartbeat(path: Path, stop: asyncio.Event) -> None:
    while not stop.is_set():
        await asyncio.to_thread(path.touch)
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
        except TimeoutError:
            pass


async def _maintenance(
    consumer: PipelineConsumer, redis: Redis, stop: asyncio.Event, interval_seconds: float
) -> None:
    while not stop.is_set():
        try:
            await consumer.sweep_unfinished()
        except Exception as e:
            logger.exception("worker.event_sweep_failed", error=str(e))
        try:
            await trim_acknowledged(redis)
        except Exception as e:
            logger.exception("worker.stream_trim_failed", error=str(e))
        try:
            await run_once(
                consumer.engine,
                "redaction_sweep",
                interval_gap(interval_seconds),
                redaction.sweep_pending,
                consumer.engine,
            )
        except Exception as e:
            logger.exception("worker.redaction_sweep_failed", error=str(e))
        await usage.flush(consumer.engine)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
    await usage.flush(consumer.engine)


async def _serve_http(engine: AsyncEngine, redis: Redis) -> web.AppRunner | None:
    """Health, readiness and metrics on WORKER_HTTP_HOST:WORKER_HTTP_PORT."""
    settings = get_settings()
    if not settings.worker_http_port:
        return None

    async def healthz(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def readyz(_: web.Request) -> web.Response:
        results = await readiness.checks(engine, redis)
        ok = readiness.ready(results)
        return web.json_response(
            {"status": "ready" if ok else "not ready", "checks": results},
            status=200 if ok else 503,
        )

    async def prometheus(request: web.Request) -> web.Response:
        if not settings.metrics_enabled:
            return web.json_response({"detail": "Not Found"}, status=404)
        if not metrics.token_ok(request.headers.get("Authorization"), settings.metrics_token):
            return web.json_response({"detail": "Unauthorized"}, status=401)
        await metrics.collect(engine, redis, settings.audience_refresh_grace_hours)
        body, content_type = metrics.exposition()
        return web.Response(body=body, headers={"Content-Type": content_type})

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    app.router.add_get("/metrics", prometheus)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, settings.worker_http_host, settings.worker_http_port).start()
    logger.info(
        "worker.http_started", host=settings.worker_http_host, port=settings.worker_http_port
    )
    return runner


async def _probe_embedding_width(embedder: Any) -> None:
    from checkcord.pipeline.embedder import EmbeddingDimensionError

    try:
        await embedder.embed_text("checkcord")
    except EmbeddingDimensionError as e:
        raise StartupConfigError(str(e)) from e
    except Exception as e:
        logger.warning("worker.embedding_probe_failed", error=str(e))


async def main(heartbeat_file: Path | None = None) -> None:
    from checkcord.pipeline.embedder import EmbeddingClient

    settings = get_settings()
    validate_security_settings(settings)
    validate_worker_settings(settings)
    llm_client = LLMClient.from_settings(settings)

    engine = create_checkcord_engine(settings.database_url, echo=False)
    try:
        await enforce_database_role(settings, engine)
        await enforce_embedding_column(settings, engine)
    except BaseException:
        await engine.dispose()
        raise

    redis = Redis.from_url(settings.redis_url)
    bus = RedisStreamsEventBus(redis)

    embedder = None
    embeddings_provider = build_embeddings_provider(settings)
    if embeddings_provider is not None:
        embedder = EmbeddingClient(
            embeddings_provider, purpose="ingest", dimension=settings.embedding_dimension
        )
        await _probe_embedding_width(embedder)

    enricher = PipelineEnricher(bus, engine)
    consumer = PipelineConsumer(bus, engine, embedder=embedder)
    scrubber = PipelineScrubber(bus, llm_client)
    chat_router = ChatRouter(bus, redis, llm_client, engine)
    chat_extractor = ChatExtractor(bus, redis, llm_client)
    synthesizer = SkillSynthesisWorker(bus, engine, llm_client)
    clustering_worker = ClusteringWorker(bus, engine)
    linkage_worker = LinkageWorker(
        bus, engine, llm_client, llm_timeout_seconds=settings.llm_timeout_seconds
    )
    ast_linker_worker = ASTLinkerWorker(bus, engine)
    context_compiler_worker = ContextStateCompilerWorker(redis, engine)
    transcript_worker = TranscriptExtractionWorker(bus, engine, settled_judge=llm_client)
    repo_backfill_worker = RepoBackfillWorker(bus, engine, redis)
    ast_writer_worker = ASTWriterWorker(bus, engine)
    workers = [
        enricher,
        consumer,
        scrubber,
        chat_router,
        chat_extractor,
        synthesizer,
        clustering_worker,
        linkage_worker,
        ast_linker_worker,
        context_compiler_worker,
        transcript_worker,
        repo_backfill_worker,
        ast_writer_worker,
    ]

    stop = asyncio.Event()

    def shutdown() -> None:
        logger.info("worker.stopping")
        stop.set()
        for worker in workers:
            worker.stop()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown)

    heartbeat = asyncio.create_task(_heartbeat(heartbeat_file, stop)) if heartbeat_file else None
    http = await _serve_http(engine, redis)
    maintenance = asyncio.create_task(
        _maintenance(consumer, redis, stop, settings.worker_sweep_interval_seconds)
    )

    listener_tasks = await listeners.start("worker", ListenerContext(settings, engine, bus))

    logger.info("worker.starting")
    try:
        await asyncio.gather(
            scrubber.start(["events.raw.chat"]),
            chat_router.start(["events.scrubbed.chat"]),
            chat_extractor.start(),
            enricher.start(
                [
                    "events.raw.monitoring",
                    "events.raw.agent",
                    "events.raw.tasks",
                    "events.raw.custom",
                    "events.raw.code",
                ]
            ),
            consumer.start(
                [
                    "events.enriched.chat",
                    "events.enriched.monitoring",
                    "events.enriched.agent",
                ]
            ),
            synthesizer.start(["events.enriched.agent"]),
            clustering_worker.start(["events.clustering.pending"]),
            linkage_worker.start(["events.linkage.pending"]),
            ast_linker_worker.start(["contexts.created"]),
            context_compiler_worker.start(["contexts.created", AUDIENCE_TOPIC]),
            transcript_worker.start(["events.raw.meetings"]),
            repo_backfill_worker.start(["repo.backfill.requested"]),
            ast_writer_worker.start(["ci.pr.merged"]),
        )
    finally:
        stop.set()
        await listeners.stop(listener_tasks)
        if heartbeat:
            await heartbeat
        await maintenance
        if http is not None:
            await http.cleanup()
        logger.info("worker.closing_connections")
        await redis.aclose()
        await engine.dispose()


def run(heartbeat_file: Path | None = None) -> None:
    settings = get_settings()
    setup_logging(log_level=settings.log_level, json_format=settings.environment != "development")
    asyncio.run(main(heartbeat_file))
