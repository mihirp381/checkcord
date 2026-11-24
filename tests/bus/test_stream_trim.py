import pytest
from redis.asyncio import Redis

from checkcord.bus.stream_trim import acknowledged_below, trim_acknowledged


async def _fill(redis: Redis, stream: str, n: int) -> list[bytes]:
    return [await redis.xadd(stream, {"n": str(i)}) for i in range(n)]


async def _read(redis: Redis, stream: str, group: str, count: int) -> list[bytes]:
    result = await redis.xreadgroup(group, "c1", {stream: ">"}, count=count)
    return [entry_id for entry_id, _ in result[0][1]] if result else []


async def _remaining(redis: Redis, stream: str) -> list[bytes]:
    return [entry_id for entry_id, _ in await redis.xrange(stream)]


@pytest.mark.asyncio
async def test_stream_without_groups_is_left_alone(async_redis_client: Redis) -> None:
    await _fill(async_redis_client, "s.nogroup", 5)
    assert await acknowledged_below(async_redis_client, "s.nogroup") is None
    assert await trim_acknowledged(async_redis_client) == {}
    assert await async_redis_client.xlen("s.nogroup") == 5


@pytest.mark.asyncio
async def test_trims_only_below_oldest_unacknowledged(async_redis_client: Redis) -> None:
    r = async_redis_client
    ids = await _fill(r, "s.one", 6)
    await r.xgroup_create("s.one", "g", id="0")
    read = await _read(r, "s.one", "g", 4)
    await r.xack("s.one", "g", read[0], read[1], read[3])

    assert await trim_acknowledged(r) == {"s.one": 2}
    assert await _remaining(r, "s.one") == ids[2:]


@pytest.mark.asyncio
async def test_slowest_group_bounds_the_trim(async_redis_client: Redis) -> None:
    r = async_redis_client
    ids = await _fill(r, "s.two", 5)
    await r.xgroup_create("s.two", "fast", id="0")
    await r.xgroup_create("s.two", "slow", id="0")
    await r.xack("s.two", "fast", *await _read(r, "s.two", "fast", 5))
    await r.xack("s.two", "slow", *await _read(r, "s.two", "slow", 1))

    await trim_acknowledged(r)
    assert await _remaining(r, "s.two") == ids[1:]


@pytest.mark.asyncio
async def test_undelivered_entries_are_kept(async_redis_client: Redis) -> None:
    r = async_redis_client
    ids = await _fill(r, "s.three", 3)
    await r.xgroup_create("s.three", "g", id="0")
    await r.xack("s.three", "g", *await _read(r, "s.three", "g", 1))

    await trim_acknowledged(r)
    assert await _remaining(r, "s.three") == ids[1:]


@pytest.mark.asyncio
async def test_dead_letter_streams_are_not_trimmed(async_redis_client: Redis) -> None:
    r = async_redis_client
    await _fill(r, "s.four.dlq", 3)
    await r.xgroup_create("s.four.dlq", "g", id="$")
    await trim_acknowledged(r)
    assert await r.xlen("s.four.dlq") == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_redis_keeps_every_unacknowledged_entry(async_redis_client: Redis) -> None:
    r = async_redis_client
    ids = await _fill(r, "s.real", 10)
    for group in ("pipeline", "audit"):
        await r.xgroup_create("s.real", group, id="0")
    pipeline = await _read(r, "s.real", "pipeline", 6)
    await r.xack("s.real", "pipeline", *pipeline[:3], *pipeline[4:])
    audit = await _read(r, "s.real", "audit", 8)
    await r.xack("s.real", "audit", *audit)

    assert await trim_acknowledged(r) == {"s.real": 3}
    assert await _remaining(r, "s.real") == ids[3:]
    _, claimed, _ = await r.xautoclaim("s.real", "pipeline", "c2", min_idle_time=0, start_id="0")
    assert [entry_id for entry_id, _ in claimed] == [ids[3]]
    assert [e for e, _ in (await r.xreadgroup("pipeline", "c2", {"s.real": ">"}))[0][1]] == ids[6:]
