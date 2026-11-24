"""Start and stop the connectors' long-running listeners in one process."""

import asyncio
from typing import Literal

import structlog

from checkcord.connectors.base import ListenerContext
from checkcord.connectors.registry import connectors

logger = structlog.get_logger(__name__)


async def start(
    process: Literal["api", "worker"], ctx: ListenerContext
) -> list[asyncio.Task[None]]:
    tasks: list[asyncio.Task[None]] = []
    for c in connectors():
        if c.listener is not None and c.listener.process == process:
            tasks.append(asyncio.create_task(c.listener.run(ctx), name=f"listener:{c.name}"))
    await asyncio.sleep(0)
    return tasks


async def stop(tasks: list[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("listener_failed", listener=task.get_name(), error=str(e))
