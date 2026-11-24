"""Readiness: Postgres answers, its schema is at the migration head, and Redis answers."""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any

from sqlalchemy import text

from checkcord.services.doctor import alembic_heads, find_alembic_ini

TIMEOUT_SECONDS = 3.0


@lru_cache(maxsize=1)
def migration_heads() -> frozenset[str]:
    ini = find_alembic_ini()
    return frozenset(alembic_heads(ini)) if ini is not None else frozenset()


async def _database(engine: Any) -> str:
    async with engine.connect() as conn:
        current = {
            r[0] for r in await conn.execute(text("SELECT version_num FROM alembic_version"))
        }
    heads = migration_heads()
    if not heads:
        return "alembic.ini not found"
    if current != set(heads):
        return f"schema at {', '.join(sorted(current)) or 'nothing'}, head is {', '.join(heads)}"
    return "ok"


async def _redis(redis: Any) -> str:
    await redis.ping()
    return "ok"


async def checks(engine: Any, redis: Any) -> dict[str, str]:
    """``"ok"`` for each part that is ready, else the reason."""
    parts = {"postgres": _database(engine) if engine is not None else None}
    parts["redis"] = _redis(redis) if redis is not None else None
    results: dict[str, str] = {}
    for name, part in parts.items():
        if part is None:
            results[name] = "not configured"
            continue
        try:
            results[name] = await asyncio.wait_for(part, TIMEOUT_SECONDS)
        except Exception as err:
            results[name] = f"unreachable: {type(err).__name__}"
    return results


def ready(results: dict[str, str]) -> bool:
    return all(value == "ok" for value in results.values())
