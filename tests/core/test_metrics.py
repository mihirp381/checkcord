import socket
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import aiohttp
import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.core import metrics, telemetry
from checkcord.core.config import get_settings
from checkcord.llm import usage
from checkcord.main import create_app
from checkcord.services import readiness

REQUIRED = {
    "checkcord_http_requests",
    "checkcord_http_request_duration_seconds",
    "checkcord_events_ingested",
    "checkcord_pipeline_stage",
    "checkcord_stream_messages",
    "checkcord_events_unfinished",
    "checkcord_stream_lag",
    "checkcord_stream_pending",
    "checkcord_dead_letter_entries",
    "checkcord_chat_open",
    "checkcord_model_calls",
    "checkcord_model_tokens",
    "checkcord_audience_containers_stale",
    "checkcord_scheduled_job_last_success_timestamp_seconds",
    "checkcord_sign_in_failures",
    "checkcord_audit_write_failures",
    "checkcord_auth_failures",
    "checkcord_rate_limited",
}
ALLOWED_LABELS = {
    "route",
    "status",
    "source",
    "stream",
    "group",
    "outcome",
    "counter",
    "stage",
    "platform",
    "error",
    "state",
    "kind",
    "purpose",
    "model",
    "direction",
    "job",
    "channel",
    "reason",
    "limit",
    "provider",
    "le",
}


def _client(redis: Any = None) -> TestClient:
    app = create_app()
    app.state.ctx = SimpleNamespace(redis_client=redis, db_engine=None)
    return TestClient(app)


def _enable(monkeypatch: pytest.MonkeyPatch, token: str | None) -> None:
    monkeypatch.setenv("METRICS_ENABLED", "true")
    if token:
        monkeypatch.setenv("METRICS_TOKEN", token)
    get_settings.cache_clear()


def test_metrics_are_off_by_default() -> None:
    assert _client().get("/metrics").status_code == 404


def test_metrics_token_is_required_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "scrape-" + uuid4().hex
    _enable(monkeypatch, token)
    client = _client()
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401
    ok = client.get("/metrics", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("text/plain")


def test_metrics_list_and_labels_carry_nothing_personal(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, None)
    tenant, email, key = uuid4(), f"person-{uuid4().hex[:6]}@example.com", "chk_" + uuid4().hex
    client = _client(fakeredis.aioredis.FakeRedis())
    client.get(f"/api/v1/graph/{uuid4()}", headers={"Authorization": f"Bearer {key}"})
    client.post("/mcp", headers={"Authorization": f"Bearer {key}"})
    telemetry.increment_counter(
        "scrubber.failure",
        {"stage": "scrub", "platform": "slack", "tenant_id": str(tenant), "user": email},
    )
    usage.record("chat", "distill", "gpt-test", input_tokens=3, output_tokens=2, tenant_id=tenant)
    metrics.EVENTS_INGESTED.labels("slack").inc()

    body = client.get("/metrics").text
    families = {f.name: f for f in text_string_to_metric_families(body)}
    assert REQUIRED <= families.keys()
    for name, family in families.items():
        if not name.startswith("checkcord_"):
            continue
        for sample in family.samples:
            assert set(sample.labels) <= ALLOWED_LABELS, (name, sample.labels)
    for secret in (str(tenant), email, key):
        assert secret not in body
    routes = {s.labels.get("route") for s in families["checkcord_http_requests"].samples}
    assert "/mcp" in routes and not any(r and r.count("-") >= 4 for r in routes)


@pytest.mark.asyncio
async def test_open_chat_buffers_and_discussions_are_counted() -> None:
    from checkcord.storage.redis_trees import ACTIVE_BUFFERS_SET, ACTIVE_TREES_ZSET

    redis = fakeredis.aioredis.FakeRedis()
    await redis.sadd(ACTIVE_BUFFERS_SET, "a", "b")
    await redis.zadd(ACTIVE_TREES_ZSET, {"t1": 1.0})
    await metrics.collect(None, redis, 24)
    assert metrics.CHAT_OPEN.labels("buffers")._value.get() == 2
    assert metrics.CHAT_OPEN.labels("discussions")._value.get() == 1
    await redis.srem(ACTIVE_BUFFERS_SET, "a", "b")
    await redis.zrem(ACTIVE_TREES_ZSET, "t1")
    await metrics.collect(None, redis, 24)
    assert metrics.CHAT_OPEN.labels("buffers")._value.get() == 0


@pytest.mark.asyncio
async def test_readiness_names_the_part_that_is_down() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    engine = create_async_engine(f"postgresql+asyncpg://u:p@127.0.0.1:{closed}/db")
    try:
        results = await readiness.checks(engine, fakeredis.aioredis.FakeRedis())
    finally:
        await engine.dispose()
    assert results["redis"] == "ok"
    assert results["postgres"].startswith("unreachable")
    assert not readiness.ready(results)

    from redis.asyncio import Redis

    redis = Redis(host="127.0.0.1", port=closed)
    results = await readiness.checks(None, redis)
    await redis.aclose()
    assert results["redis"].startswith("unreachable")
    assert results["postgres"] == "not configured"


def test_readyz_answers_503_when_not_ready() -> None:
    response = _client().get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "not ready"
    assert _client().get("/healthz").status_code == 200


@pytest.mark.asyncio
async def test_worker_http_serves_health_and_guards_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from checkcord import worker

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    token = "scrape-" + uuid4().hex
    monkeypatch.setenv("WORKER_HTTP_PORT", str(port))
    get_settings.cache_clear()
    runner = await worker._serve_http(None, fakeredis.aioredis.FakeRedis())
    assert runner is not None
    base = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"{base}/healthz") as resp:
                assert resp.status == 200
            async with http.get(f"{base}/readyz") as resp:
                assert resp.status == 503
            async with http.get(f"{base}/metrics") as resp:
                assert resp.status == 404
            monkeypatch.setenv("METRICS_ENABLED", "true")
            monkeypatch.setenv("METRICS_TOKEN", token)
            get_settings.cache_clear()
    finally:
        await runner.cleanup()

    runner = await worker._serve_http(None, fakeredis.aioredis.FakeRedis())
    assert runner is not None
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"{base}/metrics") as resp:
                assert resp.status == 401
            headers = {"Authorization": f"Bearer {token}"}
            async with http.get(f"{base}/metrics", headers=headers) as resp:
                assert resp.status == 200
                assert "checkcord_stream_messages_total" in await resp.text()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_worker_http_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from checkcord import worker

    monkeypatch.setenv("WORKER_HTTP_PORT", "0")
    get_settings.cache_clear()
    assert await worker._serve_http(None, None) is None
