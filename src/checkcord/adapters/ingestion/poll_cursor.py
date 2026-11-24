"""Per-installation polling position for the task pollers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.storage.postgres import app_bypass_rls_context

MAX_PAGES = 20
FIRST_POLL_LOOKBACK = timedelta(days=7)


def start(cursor: datetime | None) -> datetime:
    return cursor if cursor is not None else datetime.now(UTC) - FIRST_POLL_LOOKBACK


async def save(engine: AsyncEngine, model: Any, row_id: Any, **values: Any) -> None:
    async with app_bypass_rls_context(engine) as conn:
        await conn.execute(
            update(model).where(model.id == row_id).values(updated_at=model.updated_at, **values)
        )


def parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
