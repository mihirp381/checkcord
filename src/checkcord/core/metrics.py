"""Prometheus metrics. Labels are fixed sets: never a person, key, tenant or record content."""

from __future__ import annotations

import asyncio
import hmac
from typing import Any

import structlog
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

logger = structlog.get_logger(__name__)

HTTP_REQUESTS = Counter(
    "checkcord_http_requests_total",
    "API requests by route template and status",
    ["route", "status"],
)
HTTP_DURATION = Histogram(
    "checkcord_http_request_duration_seconds", "API request latency by route template", ["route"]
)
EVENTS_INGESTED = Counter(
    "checkcord_events_ingested_total", "Events saved by the worker, by source", ["source"]
)
EVENTS_SKIPPED_REMOVED = Counter(
    "checkcord_events_skipped_removed_total",
    "Events not stored because an admin removed the same item before, by platform",
    ["platform"],
)
STREAM_MESSAGES = Counter(
    "checkcord_stream_messages_total",
    "Messages handled per pipeline stage (stream and consumer group): ok, failed or dead_letter",
    ["stream", "group", "outcome"],
)
PIPELINE_STAGE = Counter(
    "checkcord_pipeline_stage_total",
    "Outcomes inside pipeline stages",
    ["counter", "stage", "platform", "error"],
)
EVENTS_UNFINISHED = Gauge(
    "checkcord_events_unfinished", "Events not yet embedded: pending or failed", ["state"]
)
STREAM_LAG = Gauge(
    "checkcord_stream_lag", "Entries a consumer group has not read yet", ["stream", "group"]
)
STREAM_PENDING = Gauge(
    "checkcord_stream_pending", "Entries read but not acknowledged", ["stream", "group"]
)
DEAD_LETTERS = Gauge("checkcord_dead_letter_entries", "Entries in a dead-letter stream", ["stream"])
CHAT_OPEN = Gauge(
    "checkcord_chat_open",
    "Chat channel buffers and discussions not yet handed to extraction",
    ["kind"],
)
CHAT_DISCUSSIONS_WAITING = Gauge(
    "checkcord_chat_discussions_waiting", "Chat discussions waiting for extraction"
)
CHAT_DISCUSSIONS_FAILED = Gauge(
    "checkcord_chat_discussions_failed",
    "Chat discussions parked after repeated unusable extraction replies",
)
LINKAGE_SKIPPED = Counter(
    "checkcord_linkage_skipped_total",
    "Candidate records an event was kept from joining, by reason",
    ["reason"],
)
MODEL_CALLS = Counter(
    "checkcord_model_calls_total", "Model calls", ["kind", "purpose", "model", "outcome"]
)
MODEL_TOKENS = Counter(
    "checkcord_model_tokens_total", "Model tokens", ["kind", "purpose", "model", "direction"]
)
AUDIENCES_STALE = Gauge(
    "checkcord_audience_containers_stale",
    "Stored audiences not refreshed within AUDIENCE_REFRESH_GRACE_HOURS",
)
JOB_LAST_SUCCESS = Gauge(
    "checkcord_scheduled_job_last_success_timestamp_seconds",
    "When each scheduled job last succeeded, from any API process",
    ["job"],
)
JOB_LAST_FAILURE = Gauge(
    "checkcord_scheduled_job_last_failure_timestamp_seconds",
    "When each scheduled job last failed, from any API process",
    ["job"],
)
AUTH_FAILURES = Counter(
    "checkcord_auth_failures_total", "Rejected API keys and sessions", ["channel"]
)
SIGN_IN_FAILURES = Counter(
    "checkcord_sign_in_failures_total", "Refused browser sign-ins", ["reason"]
)
AUDIT_WRITE_FAILURES = Counter(
    "checkcord_audit_write_failures_total", "Audit entries that could not be written"
)
RATE_LIMITED = Counter("checkcord_rate_limited_total", "Requests answered 429", ["limit"])
RATE_LIMIT_REDIS_ERRORS = Counter(
    "checkcord_rate_limit_redis_errors_total", "Rate-limit checks that could not reach Redis"
)
PROVIDER_RATE_LIMITED = Counter(
    "checkcord_provider_rate_limited_total", "429 answers from providers, retried", ["provider"]
)

_STAGE_LABELS = ("stage", "platform", "error")
COLLECT_TIMEOUT_SECONDS = 5.0


def count_stage(name: str, tags: dict[str, Any] | None = None) -> None:
    """Count a pipeline-stage outcome; tags other than stage, platform and error are dropped."""
    tags = tags or {}
    PIPELINE_STAGE.labels(name, *(str(tags.get(label, "")) for label in _STAGE_LABELS)).inc()


def token_ok(authorization: str | None, token: str | None) -> bool:
    if not token:
        return True
    expected = f"Bearer {token}"
    return authorization is not None and hmac.compare_digest(
        authorization.encode(), expected.encode()
    )


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


async def _collect_database(engine: Any, grace_hours: int) -> None:
    from sqlalchemy import text

    from checkcord.storage.postgres import app_bypass_rls_context
    from checkcord.storage.scheduled_jobs import last_runs

    async with app_bypass_rls_context(engine) as conn:
        counts = {"pending": 0, "failed": 0}
        rows = await conn.execute(
            text(
                "SELECT pipeline_state, count(*) FROM events "
                "WHERE pipeline_state IN ('pending', 'failed') GROUP BY pipeline_state"
            )
        )
        counts.update({state: n for state, n in rows})
        for state, n in counts.items():
            EVENTS_UNFINISHED.labels(state).set(n)
        stale = await conn.scalar(
            text(
                "SELECT count(*) FROM audience_containers WHERE refreshed_at IS NULL "
                "OR refreshed_at < now() - make_interval(hours => :grace)"
            ),
            {"grace": grace_hours},
        )
        AUDIENCES_STALE.set(stale or 0)
        for job, (succeeded, failed) in (await last_runs(conn)).items():
            if succeeded is not None:
                JOB_LAST_SUCCESS.labels(job).set(succeeded.timestamp())
            if failed is not None:
                JOB_LAST_FAILURE.labels(job).set(failed.timestamp())


async def _collect_streams(redis: Any) -> None:
    from checkcord.bus.dead_letter import DLQ_SUFFIX
    from checkcord.storage.redis_trees import (
        ACTIVE_BUFFERS_SET,
        ACTIVE_TREES_ZSET,
        FAILED_TREES_ZSET,
    )

    CHAT_OPEN.labels("buffers").set(await redis.scard(ACTIVE_BUFFERS_SET))
    CHAT_OPEN.labels("discussions").set(await redis.zcard(ACTIVE_TREES_ZSET))
    CHAT_DISCUSSIONS_WAITING.set(await redis.zcard(ACTIVE_TREES_ZSET))
    CHAT_DISCUSSIONS_FAILED.set(await redis.zcard(FAILED_TREES_ZSET))

    async for key in redis.scan_iter(_type="stream"):
        stream = _text(key)
        if stream.endswith(DLQ_SUFFIX):
            DEAD_LETTERS.labels(stream).set(await redis.xlen(stream))
            continue
        for group in await redis.xinfo_groups(stream):
            info = {_text(k): v for k, v in group.items()}
            name = _text(info["name"])
            STREAM_PENDING.labels(stream, name).set(int(info.get("pending") or 0))
            lag = info.get("lag")
            if lag is not None:
                STREAM_LAG.labels(stream, name).set(int(lag))


async def collect(engine: Any, redis: Any, grace_hours: int) -> None:
    """Refresh the gauges read from Postgres and Redis; a part that fails keeps its last value."""
    parts = []
    if engine is not None:
        parts.append(("database", _collect_database(engine, grace_hours)))
    if redis is not None:
        parts.append(("redis", _collect_streams(redis)))
    for name, part in parts:
        try:
            await asyncio.wait_for(part, COLLECT_TIMEOUT_SECONDS)
        except Exception as err:
            logger.warning("metrics.collect_failed", part=name, error=str(err))


def exposition() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
