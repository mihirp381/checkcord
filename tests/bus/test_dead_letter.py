import json

import pytest
from redis.asyncio import Redis

from checkcord.bus import dead_letter

FAKE_PAT = "ghp_" + "A1b2C3d4" * 4


def test_fields_keep_payload_and_redact_error() -> None:
    raw = {"payload": json.dumps({"a": 1}), "personal_access_token": FAKE_PAT}
    fields = dead_letter.dead_letter_fields(raw, f"Traceback\nValueError: bad {FAKE_PAT}")
    assert fields["payload"] == raw["payload"]
    assert "personal_access_token" not in fields
    assert FAKE_PAT not in fields[dead_letter.ERROR_FIELD]
    assert "[REDACTED:TOKEN]" in fields[dead_letter.ERROR_FIELD]


def test_old_error_field_is_replaced() -> None:
    fields = dead_letter.dead_letter_fields({"a": "1", "dlq_error": "old"}, "new")
    assert fields == {"a": "1", "dlq_error": "new"}


@pytest.mark.parametrize("name", ["events.raw.custom", ".dlq", "x.dlq.extra"])
def test_source_stream_refuses_non_dlq_names(name: str) -> None:
    with pytest.raises(ValueError):
        dead_letter.source_stream(name)


def test_source_stream_and_age() -> None:
    assert dead_letter.source_stream("events.raw.custom.dlq") == "events.raw.custom"
    assert dead_letter.entry_age_seconds("1000-0", now=61.0) == 60.0
    assert (
        dead_letter.last_error_line("Traceback:\n  frame\nRuntimeError: x\n") == "RuntimeError: x"
    )


@pytest.mark.asyncio
async def test_list_show_replay_drop(async_redis_client: Redis) -> None:
    r = async_redis_client
    for i in range(3):
        await dead_letter.write_dead_letter(r, "t.src", {"n": str(i)}, f"err {i}")
    await r.xadd("t.other", {"n": "x"})

    [stream] = await dead_letter.list_streams(r)
    assert (stream.name, stream.entries) == ("t.src.dlq", 3)

    letters = await dead_letter.entries(r, "t.src.dlq")
    assert [entry.error for entry in letters] == ["err 0", "err 1", "err 2"]
    assert len(await dead_letter.entries(r, "t.src.dlq", limit=2)) == 2

    first = letters[0].entry_id
    assert await dead_letter.replay(r, "t.src.dlq", [first, "1-1"]) == [first]
    [(_, replayed)] = await r.xrange("t.src")
    assert replayed == {b"n": b"0"}

    assert await dead_letter.drop(r, "t.src.dlq") == [e.entry_id for e in letters[1:]]
    assert await r.xlen("t.src.dlq") == 0
    assert await r.xlen("t.other") == 1


@pytest.mark.asyncio
async def test_entries_pages_through_long_streams(async_redis_client: Redis) -> None:
    for i in range(250):
        await async_redis_client.xadd("t.long.dlq", {"n": str(i), "dlq_error": "e"})
    letters = await dead_letter.entries(async_redis_client, "t.long.dlq")
    assert [entry.fields["n"] for entry in letters] == [str(i) for i in range(250)]


def test_last_error_line_finds_the_exception_in_a_traceback() -> None:
    trace = (
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1, in f\n'
        "pydantic_core._pydantic_core.ValidationError: 1 validation error for CanonicalEvent\n"
        "not\n"
        "  Extra inputs are not permitted\n"
        "    For further information visit https://errors.pydantic.dev/v/extra_forbidden\n"
    )
    assert dead_letter.last_error_line(trace).startswith("pydantic_core._pydantic_core.Validation")


@pytest.mark.asyncio
async def test_emptied_streams_are_not_listed(async_redis_client: Redis) -> None:
    await dead_letter.write_dead_letter(async_redis_client, "t.gone", {"a": "1"}, "e")
    await dead_letter.drop(async_redis_client, "t.gone.dlq")
    assert await dead_letter.list_streams(async_redis_client) == []
