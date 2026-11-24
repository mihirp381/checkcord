"""Several API and worker processes against one Postgres and one Redis."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.api import rate_limit
from checkcord.api.deps import Principal
from checkcord.core.config import get_settings
from checkcord.services import readiness
from checkcord.storage.postgres import create_checkcord_engine
from checkcord.storage.scheduled_jobs import LOCK_NAMESPACE, lock_key, run_once
from checkcord.workers.pr_status_reporter import PRStatusWorker

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture
async def engines() -> AsyncIterator[tuple[AsyncEngine, AsyncEngine]]:
    """Two engines, as two processes would have."""
    url = get_settings().database_url
    first, second = create_checkcord_engine(url), create_checkcord_engine(url)
    yield first, second
    await first.dispose()
    await second.dispose()


async def _job_row(engine: AsyncEngine, job_id: str) -> Any:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT * FROM scheduled_jobs WHERE job_id = :job"), {"job": job_id}
            )
        ).one_or_none()


async def _forget(engine: AsyncEngine, job_id: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM scheduled_jobs WHERE job_id = :job"), {"job": job_id})


def _raw_url() -> str:
    return (
        make_url(get_settings().database_url).set(drivername="postgresql").render_as_string(False)
    )


async def test_two_schedulers_run_a_job_once_per_interval(
    engines: tuple[AsyncEngine, AsyncEngine],
) -> None:
    job_id, gap, runs = f"test-job-{uuid4().hex[:8]}", 1.0, []

    async def job() -> None:
        runs.append(time.monotonic())
        await asyncio.sleep(0.2)

    async def scheduler(engine: AsyncEngine) -> None:
        for _ in range(9):
            await run_once(engine, job_id, gap, job)
            await asyncio.sleep(0.4)

    try:
        await asyncio.gather(scheduler(engines[0]), scheduler(engines[1]))
        assert 3 <= len(runs) <= 5
        assert all(b - a >= gap - 0.05 for a, b in zip(runs, runs[1:], strict=False))
        row = await _job_row(engines[0], job_id)
        assert row.last_succeeded_at is not None and row.last_failed_at is None
    finally:
        await _forget(engines[0], job_id)


async def test_concurrent_ticks_run_once(engines: tuple[AsyncEngine, AsyncEngine]) -> None:
    job_id, calls = f"test-job-{uuid4().hex[:8]}", []

    async def job() -> None:
        calls.append(1)
        await asyncio.sleep(0.3)

    try:
        done = await asyncio.gather(*(run_once(engines[i % 2], job_id, 60, job) for i in range(6)))
        assert calls == [1]
        assert done.count(True) == 1
        assert await run_once(engines[1], job_id, 60, job) is False
        assert calls == [1]
    finally:
        await _forget(engines[0], job_id)


async def test_a_dead_holder_does_not_block_the_next_run(
    engines: tuple[AsyncEngine, AsyncEngine],
) -> None:
    job_id, calls = f"test-job-{uuid4().hex[:8]}", []

    async def job() -> None:
        calls.append(1)

    holder = await asyncpg.connect(_raw_url())
    try:
        assert await holder.fetchval(
            "SELECT pg_try_advisory_lock($1, $2)", LOCK_NAMESPACE, lock_key(job_id)
        )
        await holder.execute(
            "INSERT INTO scheduled_jobs (job_id, last_started_at) "
            "VALUES ($1, now() - interval '10 minutes')",
            job_id,
        )
        assert await run_once(engines[0], job_id, 60, job) is False
        assert calls == []

        pid = await holder.fetchval("SELECT pg_backend_pid()")
        admin = await asyncpg.connect(_raw_url())
        try:
            await admin.execute("SELECT pg_terminate_backend($1)", pid)
        finally:
            await admin.close()
        await asyncio.sleep(0.2)

        assert await run_once(engines[1], job_id, 60, job) is True
        assert calls == [1]
    finally:
        if not holder.is_closed():
            await holder.close()
        await _forget(engines[0], job_id)


async def test_a_failed_run_is_recorded_and_releases_the_lock(
    engines: tuple[AsyncEngine, AsyncEngine],
) -> None:
    job_id = f"test-job-{uuid4().hex[:8]}"

    async def job() -> None:
        raise RuntimeError("provider down")

    try:
        assert await run_once(engines[0], job_id, 0, job) is False
        row = await _job_row(engines[0], job_id)
        assert row.last_failed_at is not None and row.last_succeeded_at is None
        ran = AsyncMock()
        assert await run_once(engines[1], job_id, 0, ran) is True
        ran.assert_awaited_once()
    finally:
        await _forget(engines[0], job_id)


async def test_two_workers_sweep_stored_redaction_once(
    engines: tuple[AsyncEngine, AsyncEngine],
) -> None:
    sweeps = []

    async def sweep(engine: AsyncEngine) -> None:
        sweeps.append(engine)
        await asyncio.sleep(0.3)

    try:
        await _forget(engines[0], "redaction_sweep")
        await asyncio.gather(
            run_once(engines[0], "redaction_sweep", 30, sweep, engines[0]),
            run_once(engines[1], "redaction_sweep", 30, sweep, engines[1]),
        )
        assert len(sweeps) == 1
    finally:
        await _forget(engines[0], "redaction_sweep")


async def test_two_api_processes_report_a_pr_fallback_once(async_redis_client: Redis) -> None:
    item = json.dumps(
        {
            "sha": uuid4().hex,
            "owner": "acme",
            "repo": "api",
            "pr_number": 7,
            "tenant_id": str(uuid4()),
        }
    )
    sha = json.loads(item)["sha"]
    await async_redis_client.set(f"ci:pr_status:{sha}", "pending")
    await async_redis_client.zadd("ci.pr.fallback_zset", {item: time.time() - 1})
    github = MagicMock()
    github.create_commit_status = AsyncMock()

    workers = [PRStatusWorker(async_redis_client, MagicMock()) for _ in range(2)]
    with (
        patch("checkcord.workers.pr_status_reporter.github_token_for", AsyncMock(return_value="t")),
        patch("checkcord.workers.pr_status_reporter.GitHubRestClient", return_value=github),
    ):
        tasks = [asyncio.create_task(w._poll_fallback_tasks()) for w in workers]
        await asyncio.sleep(1.5)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    github.create_commit_status.assert_awaited_once()
    assert await async_redis_client.zcard("ci.pr.fallback_zset") == 0


def _request(redis: Redis, host: str = "198.51.100.4") -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(ctx=SimpleNamespace(redis_client=redis))),
        client=SimpleNamespace(host=host),
        state=SimpleNamespace(),
    )


async def test_rate_limits_are_shared_through_redis(
    async_redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "4")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURES_PER_MINUTE", "3")
    get_settings.cache_clear()
    clock = {"now": 1_899_999_960.0 + 5}
    principal = Principal(tenant_id=uuid4(), key_name="k", key_id=uuid4())
    host = f"198.51.100.{uuid4().int % 200 + 1}"
    second_process = Redis.from_url(get_settings().redis_url)
    try:
        with patch.object(rate_limit.time, "time", lambda: clock["now"]):
            for redis in (async_redis_client, second_process, async_redis_client, second_process):
                await rate_limit.principal_request(_request(redis), principal)
            with pytest.raises(HTTPException) as refused:
                await rate_limit.principal_request(_request(second_process), principal)
            assert refused.value.status_code == 429
            assert refused.value.headers == {"Retry-After": "55"}

            for redis in (async_redis_client, second_process, async_redis_client):
                assert await rate_limit.failing_source(_request(redis, host)) is None
                await rate_limit.count_failure(_request(redis, host))
            assert await rate_limit.failing_source(_request(second_process, host)) == 55

            clock["now"] += 60
            await rate_limit.principal_request(_request(second_process), principal)
            assert await rate_limit.failing_source(_request(second_process, host)) is None
    finally:
        await second_process.aclose()


async def test_readiness_is_ready_with_postgres_and_redis_at_head(
    async_redis_client: Redis,
) -> None:
    engine = create_checkcord_engine(get_settings().database_url)
    try:
        results = await readiness.checks(engine, async_redis_client)
        assert results == {"postgres": "ok", "redis": "ok"}
        with patch.object(readiness, "migration_heads", lambda: frozenset({"ffffffffffff"})):
            behind = await readiness.checks(engine, async_redis_client)
        assert behind["postgres"].startswith("schema at ")
        assert not readiness.ready(behind)
    finally:
        await engine.dispose()


async def test_doctor_flags_an_app_role_that_can_change_audit_entries() -> None:
    from checkcord.services.doctor import audit_grants_check

    role, password = f"audit_probe_{uuid4().hex[:8]}", uuid4().hex
    admin = create_checkcord_engine(get_settings().database_url)
    url = make_url(get_settings().database_url).set(username=role, password=password)
    probe = create_checkcord_engine(url.render_as_string(False))
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'"))
            await conn.execute(text(f"GRANT SELECT, INSERT ON audit_log_entries TO {role}"))
        assert await audit_grants_check(admin) is None
        check = await audit_grants_check(probe)
        assert check is not None and check.ok

        async with admin.begin() as conn:
            await conn.execute(text(f"GRANT UPDATE ON audit_log_entries TO {role}"))
        check = await audit_grants_check(probe)
        assert check is not None and not check.ok and "REVOKE" in check.detail
    finally:
        await probe.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"REVOKE ALL ON audit_log_entries FROM {role}"))
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        await admin.dispose()
