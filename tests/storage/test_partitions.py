"""Weekly partition maintenance for the `events` table."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from app_role_helpers import create_app_role, drop_app_role
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.core.config import get_settings
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.pipeline.garbage_collector import maintain_partitions
from checkcord.storage.partitions import create_weekly_partitions, drop_empty_partitions
from checkcord.storage.postgres import PostgresEventStore, tenant_context

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_create_weekly_partitions_is_idempotent() -> None:
    """Running twice must not raise and must return the same partition names both times."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    try:
        first = await create_weekly_partitions(engine, weeks_ahead=2)
        second = await create_weekly_partitions(engine, weeks_ahead=2)

        assert first == second
        assert len(first) == 2

        async with engine.begin() as conn:
            for name in first:
                res = await conn.execute(
                    text("SELECT to_regclass(:name) IS NOT NULL"), {"name": name}
                )
                assert res.scalar() is True
    finally:
        await engine.dispose()


async def test_partition_names_align_across_iso_year_boundary() -> None:
    """Dec 30, 2025 falls in ISO week 1 of 2026 -- the partition name must use the ISO year,
    not the calendar year of "today", exactly the kind of boundary bug this heuristic exists
    to avoid getting wrong.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    fake_today = datetime(2025, 12, 30, tzinfo=UTC)
    created: list[str] = []

    try:
        with patch("checkcord.storage.partitions.datetime") as mock_dt:
            mock_dt.now.return_value = fake_today
            created = await create_weekly_partitions(engine, weeks_ahead=4)

        assert created == [
            "events_2026_w01",
            "events_2026_w02",
            "events_2026_w03",
            "events_2026_w04",
        ]

        async with engine.begin() as conn:
            for name in created:
                res = await conn.execute(
                    text("SELECT to_regclass(:name) IS NOT NULL"), {"name": name}
                )
                assert res.scalar() is True
    finally:
        async with engine.begin() as conn:
            for name in created:
                await conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
        await engine.dispose()


async def test_default_weeks_ahead_creates_four_partitions() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    try:
        created = await create_weekly_partitions(engine)
        assert len(created) == 4
    finally:
        await engine.dispose()


def _event(tenant_id: UUID, happened_at: datetime) -> CanonicalEvent:
    return CanonicalEvent(
        tenant_id=tenant_id,
        source_platform=SourcePlatform.SLACK,
        source_type=SourceType.MESSAGE,
        source_id=f"msg_{uuid4().hex[:8]}",
        happened_at=happened_at,
        author={"source_id": "user_1"},
        content_raw='{"text": "hello"}',
        content_clean="hello",
        access_control={"is_public": True, "allowed_identity_ids": []},
    )


def _partition_for(day: datetime) -> str:
    iso_year, iso_week, _ = day.date().isocalendar()
    return f"events_{iso_year}_w{iso_week:02d}"


async def _partitions_holding(engine: AsyncEngine, tenant_id: UUID) -> dict[str, str]:
    async with tenant_context(engine, tenant_id) as conn:
        rows = await conn.execute(
            text("SELECT source_id, tableoid::regclass::text FROM events WHERE tenant_id = :t"),
            {"t": tenant_id},
        )
        return {source_id: table for source_id, table in rows}


async def _exists(engine: AsyncEngine, name: str) -> bool:
    async with engine.connect() as conn:
        return bool(await conn.scalar(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": name}))


async def _event_partitions(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT inhrelid::regclass::text FROM pg_inherits "
                "WHERE inhparent = 'events'::regclass"
            )
        )
        return {name for (name,) in rows}


async def test_events_outside_weekly_partitions_land_in_default_and_move_on_maintenance() -> None:
    engine = create_async_engine(get_settings().database_url)
    tenant_id = uuid4()
    now = datetime.now(UTC)
    old = _event(tenant_id, now - timedelta(days=730))
    current = _event(tenant_id, now)
    future = _event(tenant_id, now + timedelta(days=182))
    old_partition, future_partition = (
        _partition_for(old.happened_at),
        _partition_for(future.happened_at),
    )
    assert not await _exists(engine, old_partition)
    assert not await _exists(engine, future_partition)
    partitions_before = await _event_partitions(engine)

    try:
        await create_weekly_partitions(engine, weeks_ahead=1)
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            for event in (old, current, future):
                assert await store.save_event(event) is True

        before = await _partitions_holding(engine, tenant_id)
        assert before[old.source_id] == "events_default"
        assert before[future.source_id] == "events_default"
        assert before[current.source_id] == _partition_for(now)

        for moment in (old.happened_at, future.happened_at, now):
            with patch("checkcord.storage.partitions.datetime") as mock_dt:
                mock_dt.now.return_value = moment
                created = await create_weekly_partitions(engine, weeks_ahead=1)
            assert created == [_partition_for(moment)]

        with patch("checkcord.storage.partitions.datetime") as mock_dt:
            mock_dt.now.return_value = future.happened_at
            await maintain_partitions(engine)

        after = await _partitions_holding(engine, tenant_id)
        assert after == {
            old.source_id: old_partition,
            current.source_id: _partition_for(now),
            future.source_id: future_partition,
        }
        async with engine.connect() as conn:
            leftover = await conn.scalar(
                text("SELECT count(*) FROM events_default WHERE tenant_id = :t"), {"t": tenant_id}
            )
        assert leftover == 0
    finally:
        added = await _event_partitions(engine) - partitions_before
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM events WHERE tenant_id = :t"), {"t": tenant_id})
            for name in added:
                await conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
        await engine.dispose()


async def test_new_partition_without_rows_in_default_is_created() -> None:
    engine = create_async_engine(get_settings().database_url)
    moment = datetime.now(UTC) + timedelta(days=400)
    name = _partition_for(moment)
    try:
        with patch("checkcord.storage.partitions.datetime") as mock_dt:
            mock_dt.now.return_value = moment
            assert await create_weekly_partitions(engine, weeks_ahead=1) == [name]
        async with engine.connect() as conn:
            attached = await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_inherits "
                    "WHERE inhparent = 'events'::regclass AND inhrelid = to_regclass(:n)"
                ),
                {"n": name},
            )
        assert attached == 1
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
        await engine.dispose()


async def test_retention_drops_old_partitions_only_when_empty() -> None:
    owner = create_async_engine(get_settings().database_url)
    tenant_id = uuid4()
    weeks = {"events_2009_w10": date(2009, 3, 2), "events_2009_w11": date(2009, 3, 9)}
    role, url = await create_app_role()
    app = create_async_engine(url)
    try:
        async with owner.begin() as conn:
            for name, start in weeks.items():
                await conn.execute(
                    text("SELECT checkcord_ensure_events_partition(:n, :s, :e)"),
                    {"n": name, "s": start, "e": start + timedelta(weeks=1)},
                )
        kept = _event(tenant_id, datetime(2009, 3, 4, tzinfo=UTC))
        async with tenant_context(owner, tenant_id) as conn:
            assert await PostgresEventStore(conn).save_event(kept) is True

        assert await drop_empty_partitions(app, date(2009, 3, 15)) == []
        assert await drop_empty_partitions(app, date(2009, 3, 16)) == ["events_2009_w11"]
        assert await _exists(owner, "events_2009_w10")
        assert not await _exists(owner, "events_2009_w11")
        assert (await _partitions_holding(owner, tenant_id))[kept.source_id] == "events_2009_w10"
    finally:
        await app.dispose()
        await drop_app_role(role)
        async with owner.begin() as conn:
            await conn.execute(text("DELETE FROM events WHERE tenant_id = :t"), {"t": tenant_id})
            for name in weeks:
                await conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
        await owner.dispose()
