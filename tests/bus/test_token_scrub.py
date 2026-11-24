import json
from unittest.mock import AsyncMock

import pytest

from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.bus.token_scrub import scrub_tokens


def test_flat_token_fields_removed() -> None:
    clean = scrub_tokens({"repo": "o/r", "personal_access_token": "ghp_x", "pat": "ghp_y"})
    assert clean == {"repo": "o/r"}


@pytest.mark.parametrize("key", ["payload", "data"])
def test_token_removed_from_json_body(key: str) -> None:
    body = json.dumps({"repository_full_name": "o/r", "personal_access_token": "ghp_x"})
    clean = scrub_tokens({key: body, "other": "1"})
    assert json.loads(clean[key]) == {"repository_full_name": "o/r"}
    assert clean["other"] == "1"


def test_non_json_and_tokenless_bodies_untouched() -> None:
    fields = {"payload": "not json", "data": json.dumps({"a": 1})}
    assert scrub_tokens(fields) == fields


@pytest.mark.asyncio
async def test_bus_dlq_copy_drops_legacy_token() -> None:
    redis = AsyncMock()
    bus = RedisStreamsEventBus(redis)
    raw = {"data": json.dumps({"repository_full_name": "o/r", "personal_access_token": "ghp_x"})}

    await bus._handle_dlq("ci.pr.merged", "g", "1-0", raw, "err")

    fields = redis.xadd.call_args.kwargs["fields"]
    assert "ghp_x" not in str(fields)
    assert fields["dlq_error"] == "err"
