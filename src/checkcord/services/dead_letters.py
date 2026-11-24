"""Dead-letter entries seen by one tenant.

The streams are shared by every tenant, so each entry is attributed by the
``tenant_id`` in its fields; an entry without one belongs to the default tenant.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.bus import dead_letter
from checkcord.core.secret_patterns import redact_secrets
from checkcord.storage import audit
from checkcord.storage.postgres import tenant_context


@dataclass(frozen=True)
class TenantStream:
    name: str
    entries: int
    oldest_id: str | None


def entry_tenant(fields: dict[str, str], default: UUID | None) -> UUID | None:
    raw: Any = fields.get("tenant_id")
    if raw is None and "data" in fields:
        try:
            body = json.loads(fields["data"])
            raw = body.get("tenant_id") if isinstance(body, dict) else None
        except ValueError:
            raw = None
    try:
        return UUID(str(raw)) if raw else default
    except ValueError:
        return None


def describe(letter: dead_letter.DeadLetter) -> dict[str, Any]:
    """Id, age, event id and the error line; never the message content."""
    return {
        "entry_id": letter.entry_id,
        "age_seconds": int(dead_letter.entry_age_seconds(letter.entry_id)),
        "event_id": letter.fields.get("event_id"),
        "error": redact_secrets(dead_letter.last_error_line(letter.error))[:300] or None,
    }


async def _mine(
    redis: Redis, stream: str, tenant_id: UUID, default: UUID | None
) -> list[dead_letter.DeadLetter]:
    return [
        e
        for e in await dead_letter.entries(redis, stream)
        if entry_tenant(e.fields, default) == tenant_id
    ]


async def streams(redis: Redis, tenant_id: UUID, default: UUID | None) -> list[TenantStream]:
    found = []
    for st in await dead_letter.list_streams(redis):
        mine = await _mine(redis, st.name, tenant_id, default)
        if mine:
            found.append(TenantStream(st.name, len(mine), mine[0].entry_id))
    return found


async def entries(
    redis: Redis,
    stream: str,
    tenant_id: UUID,
    default: UUID | None,
    limit: int = 100,
    after: str | None = None,
) -> tuple[list[dead_letter.DeadLetter], str | None]:
    mine = await _mine(redis, stream, tenant_id, default)
    if after is not None:
        mine = [e for e in mine if _id_key(e.entry_id) > _id_key(after)]
    page = mine[:limit]
    return page, (page[-1].entry_id if len(mine) > limit else None)


def _id_key(entry_id: str) -> tuple[int, int]:
    millis, _, seq = entry_id.partition("-")
    try:
        return int(millis), int(seq or 0)
    except ValueError as err:
        raise ValueError(f"{entry_id!r} is not a stream entry id") from err


async def act(
    engine: AsyncEngine,
    redis: Redis,
    action: str,
    stream: str,
    tenant_id: UUID,
    default: UUID | None,
    ids: list[str] | None,
    actor: audit.Actor,
) -> list[str]:
    """Replay or drop the tenant's entries; ``ids`` None means all of them."""
    mine = [e.entry_id for e in await _mine(redis, stream, tenant_id, default)]
    chosen = mine if ids is None else [i for i in ids if i in set(mine)]
    done: list[str] = []
    if chosen:
        run = dead_letter.replay if action == "replay" else dead_letter.drop
        done = await run(redis, stream, chosen)
    async with tenant_context(engine, tenant_id) as conn:
        await audit.record(
            conn,
            tenant_id,
            actor,
            f"dlq.{action}",
            params={"stream": stream, "all": ids is None},
            record_ids=done,
            result_count=len(done),
        )
    return done
