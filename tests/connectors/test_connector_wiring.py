import asyncio
import dataclasses
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from checkcord import connectors
from checkcord.connectors import Listener, ListenerContext, listeners, registry
from checkcord.core.config import get_settings
from checkcord.pipeline.scheduler import create_scheduler


def _routes(routes: Any) -> Iterator[tuple[str, frozenset[str]]]:
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from _routes(inner.routes)
        else:
            yield r.path, frozenset(getattr(r, "methods", None) or ())


def test_every_declared_router_is_mounted() -> None:
    from checkcord.main import create_app

    mounted = set(_routes(create_app().routes))
    for c in connectors.connectors():
        for router, _tag in c.routers:
            assert router.routes, c.name
            assert set(_routes(router.routes)) <= mounted, c.name


@pytest.mark.parametrize("redis", [MagicMock(), None])
def test_every_declared_poll_is_scheduled(redis: Any) -> None:
    jobs = {j.id: j for j in create_scheduler(MagicMock(), redis).get_jobs()}
    settings = get_settings()
    for c in connectors.connectors():
        if c.poll is None:
            continue
        if redis is None and c.poll.publishes:
            assert c.poll.job_id not in jobs, c.name
            continue
        job = jobs[c.poll.job_id]
        assert job.trigger.interval == timedelta(minutes=c.poll.minutes(settings)), c.name
        assert c.poll.run in job.args, c.name


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, str]]]:
    started: list[tuple[str, str]] = []

    def record(name: str, process: str) -> Listener:
        async def run(_ctx: ListenerContext) -> None:
            started.append((name, process))
            await asyncio.Event().wait()

        return Listener(run, process)

    found = {
        c.name: dataclasses.replace(c, listener=record(c.name, c.listener.process))
        if c.listener is not None
        else c
        for c in connectors.connectors()
    }
    monkeypatch.setattr(registry, "_loaded", found)
    yield started
    registry.reload()


def _declared(process: str) -> set[tuple[str, str]]:
    return {
        (c.name, process)
        for c in connectors.connectors()
        if c.listener is not None and c.listener.process == process
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("process", ["api", "worker"])
async def test_listeners_start_in_their_process(
    process: str, recorded: list[tuple[str, str]]
) -> None:
    ctx = ListenerContext(get_settings(), MagicMock(), MagicMock())
    tasks = await listeners.start(process, ctx)
    await listeners.stop(tasks)
    assert set(recorded) == _declared(process)
    assert all(t.cancelled() for t in tasks)


@pytest.mark.asyncio
async def test_api_starts_and_stops_its_listeners(recorded: list[tuple[str, str]]) -> None:
    import checkcord.main as main_mod

    @asynccontextmanager
    async def lifecycle() -> AsyncIterator[Any]:
        yield SimpleNamespace(db_engine=MagicMock(), redis_client=MagicMock())

    async def nothing(*_a: object, **_k: object) -> None:
        return None

    app = main_mod.create_app()
    with (
        patch.object(main_mod, "app_lifecycle", lifecycle),
        patch.object(main_mod, "enforce_database_role", nothing),
        patch.object(main_mod, "enforce_embedding_column", nothing),
        patch.object(main_mod, "setup_logging", lambda **_: None),
        patch("checkcord.workers.pr_evaluator.PRWorker.start", nothing),
        patch("checkcord.workers.pr_status_reporter.PRStatusWorker.start", nothing),
        patch("checkcord.llm.usage.flush", nothing),
        patch.object(main_mod, "create_scheduler", MagicMock()),
    ):
        async with main_mod.lifespan(app):
            assert set(recorded) == _declared("api")
            running = [t for t in asyncio.all_tasks() if t.get_name().startswith("listener:")]
            assert len(running) == len(_declared("api"))
        assert all(t.done() for t in running)
