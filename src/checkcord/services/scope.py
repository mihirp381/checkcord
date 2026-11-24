"""Which channels, teams, repositories, projects, pages or teams of a connection are ingested.

Connectors ask ``allows`` before they store, publish or send anything to a model. Exclude wins;
a non-empty include list means only what it lists; with neither, everything is ingested.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Date, cast, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.storage import audit
from checkcord.storage.models import ConnectionScope, ConnectionScopeSkip
from checkcord.storage.postgres import tenant_context

logger = structlog.get_logger(__name__).bind(component="scope")


def providers() -> dict[str, str]:
    """Connectors that take a scope, with what their containers are."""
    from checkcord import connectors

    return {c.name: c.scope for c in connectors.connectors() if c.scope}


CACHE_SECONDS = 10.0
_UUID_HEX = re.compile(r"[0-9a-f]{32}")

_scopes = ConnectionScope.__table__
_skips = ConnectionScopeSkip.__table__
_cache: dict[tuple[UUID, str], tuple[float, Rule]] = {}


@dataclass(frozen=True)
class Rule:
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    def allows(self, containers: Iterable[str | None]) -> bool:
        names = {normalize(c) for c in containers if c}
        if names & set(self.exclude):
            return False
        return not self.include or bool(names & set(self.include))


def normalize(name: str) -> str:
    name = name.strip().casefold()
    bare = name.replace("-", "")
    return bare if _UUID_HEX.fullmatch(bare) else name


def _clean(names: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(n for n in (normalize(x) for x in names) if n))


def supported(connection: str) -> bool:
    return connection.partition(":")[0] in providers()


def _check(connection: str) -> None:
    if not supported(connection):
        raise ValueError(
            f"{connection.partition(':')[0]!r} connections have no scope; "
            f"supported: {', '.join(providers())}"
        )


async def rule(engine: AsyncEngine, tenant_id: UUID, connection: str) -> Rule:
    async with tenant_context(engine, tenant_id) as conn:
        row = (
            await conn.execute(
                select(_scopes.c.include, _scopes.c.exclude).where(
                    (_scopes.c.tenant_id == tenant_id) & (_scopes.c.connection == connection)
                )
            )
        ).first()
    return Rule(tuple(row.include), tuple(row.exclude)) if row else Rule()


async def allows(
    engine: AsyncEngine,
    tenant_id: UUID,
    connection: str,
    *containers: str | None,
    count: bool = True,
) -> bool:
    """Whether material from these containers may be ingested. A refusal is counted."""
    key = (tenant_id, connection)
    cached = _cache.get(key)
    if cached and cached[0] > time.monotonic():
        found = cached[1]
    else:
        found = await rule(engine, tenant_id, connection)
        _cache[key] = (time.monotonic() + CACHE_SECONDS, found)
    if found.allows(containers):
        return True
    logger.info("scope.skipped", connection=connection, containers=[c for c in containers if c])
    if count:
        await _count_skip(engine, tenant_id, connection)
    return False


async def _count_skip(engine: AsyncEngine, tenant_id: UUID, connection: str) -> None:
    async with tenant_context(engine, tenant_id) as conn:
        stmt = pg_insert(_skips).values(
            tenant_id=tenant_id,
            connection=connection,
            day=cast(func.now(), Date),
            skipped=1,
        )
        await conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["tenant_id", "connection", "day"],
                set_={"skipped": _skips.c.skipped + 1},
            )
        )


async def rules(engine: AsyncEngine, tenant_id: UUID) -> dict[str, Rule]:
    async with tenant_context(engine, tenant_id) as conn:
        rows = await conn.execute(
            select(_scopes.c.connection, _scopes.c.include, _scopes.c.exclude).where(
                _scopes.c.tenant_id == tenant_id
            )
        )
        return {r.connection: Rule(tuple(r.include), tuple(r.exclude)) for r in rows.all()}


async def show(
    engine: AsyncEngine, tenant_id: UUID, connection: str, days: int = 7
) -> dict[str, Any]:
    _check(connection)
    found = await rule(engine, tenant_id, connection)
    return {
        "connection": connection,
        "include": list(found.include),
        "exclude": list(found.exclude),
        "only_listed": bool(found.include),
        "skipped": (await skipped(engine, tenant_id, days)).get(connection, 0),
        "days": days,
    }


async def change(
    engine: AsyncEngine,
    tenant_id: UUID,
    connection: str,
    actor: audit.Actor,
    *,
    include: Iterable[str] | None = None,
    exclude: Iterable[str] | None = None,
) -> Rule:
    """Replace the include and/or exclude list. Both empty removes the rule."""
    from checkcord.services.connections import connection_key, list_connections

    _check(connection)
    known = {
        connection_key(c.provider, c.identifier) for c in await list_connections(engine, tenant_id)
    }
    if connection not in known:
        raise LookupError(f"no connection {connection!r}")
    async with tenant_context(engine, tenant_id) as conn:
        row = (
            await conn.execute(
                select(_scopes.c.include, _scopes.c.exclude)
                .where((_scopes.c.tenant_id == tenant_id) & (_scopes.c.connection == connection))
                .with_for_update()
            )
        ).first()
        before = Rule(tuple(row.include), tuple(row.exclude)) if row else Rule()
        after = Rule(
            before.include if include is None else _clean(include),
            before.exclude if exclude is None else _clean(exclude),
        )
        where = (_scopes.c.tenant_id == tenant_id) & (_scopes.c.connection == connection)
        if not after.include and not after.exclude:
            await conn.execute(delete(_scopes).where(where))
        else:
            stmt = pg_insert(_scopes).values(
                tenant_id=tenant_id,
                connection=connection,
                include=list(after.include),
                exclude=list(after.exclude),
            )
            await conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=["tenant_id", "connection"],
                    set_={
                        "include": list(after.include),
                        "exclude": list(after.exclude),
                        "updated_at": func.now(),
                    },
                )
            )
        await audit.record(
            conn,
            tenant_id,
            actor,
            "connection.scope_changed",
            params={
                "connection": connection,
                "include": list(after.include),
                "exclude": list(after.exclude),
                "before": {"include": list(before.include), "exclude": list(before.exclude)},
            },
        )
    _cache.pop((tenant_id, connection), None)
    return after


async def skipped(engine: AsyncEngine, tenant_id: UUID, days: int) -> dict[str, int]:
    since = (datetime.now(UTC) - timedelta(days=days - 1)).date()
    async with tenant_context(engine, tenant_id) as conn:
        rows = await conn.execute(
            select(_skips.c.connection, func.sum(_skips.c.skipped))
            .where((_skips.c.tenant_id == tenant_id) & (_skips.c.day >= since))
            .group_by(_skips.c.connection)
        )
        return {c: int(n) for c, n in rows.all()}
