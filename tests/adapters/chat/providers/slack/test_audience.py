from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import fakeredis.aioredis
import pytest

from checkcord.adapters.chat.providers.slack.audience import resolve_slack_audience

MATCH = "checkcord.services.accounts.match"


def matching(ids: list[Any]) -> AsyncMock:
    async def match(
        engine: Any, tenant: Any, platform: str, native_ids: Any, lookup: Any = None
    ) -> Any:
        return dict(zip(native_ids, ids, strict=False)), []

    return AsyncMock(side_effect=match)


def client(is_private: bool | None, members: Any = None) -> AsyncMock:
    c = AsyncMock()
    if is_private is None:
        c.conversations_info.side_effect = RuntimeError("down")
    else:
        c.conversations_info.return_value = {"ok": True, "channel": {"is_private": is_private}}
    if isinstance(members, Exception):
        c.conversations_members.side_effect = members
    else:
        c.conversations_members.return_value = {"ok": True, "members": members or []}
    return c


async def resolve(c: AsyncMock, channel_id: str = "C1") -> Any:
    return await resolve_slack_audience(
        client=c,
        redis=fakeredis.aioredis.FakeRedis(),
        db_engine=AsyncMock(),
        tenant_id=uuid4(),
        channel_id=channel_id,
    )


@pytest.mark.asyncio
async def test_public_channel_is_public() -> None:
    audience = await resolve(client(False))
    assert audience.is_public is True
    assert audience.source_group_id == "C1"


@pytest.mark.asyncio
async def test_private_channel_lists_mapped_members() -> None:
    ids = [uuid4(), uuid4()]
    with patch(MATCH, matching(ids)) as resolver:
        audience = await resolve(client(True, ["U1", "U2"]))
    assert audience.is_public is False
    assert audience.allowed_identity_ids == sorted(ids)
    assert list(resolver.call_args.args[3]) == ["U1", "U2"]


@pytest.mark.asyncio
async def test_direct_message_is_private_without_asking() -> None:
    c = client(False, ["U1"])
    with patch(MATCH, matching([uuid4()])):
        audience = await resolve(c, channel_id="D1")
    assert audience.is_public is False
    c.conversations_info.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_private", "members"),
    [(True, RuntimeError("down")), (None, RuntimeError("down")), (True, [])],
)
async def test_unresolvable_membership_is_empty_not_public(is_private: Any, members: Any) -> None:
    with patch(MATCH, matching([uuid4()])):
        audience = await resolve(client(is_private, members))
    assert audience.is_public is False
    assert audience.allowed_identity_ids == []


@pytest.mark.asyncio
async def test_identity_failure_is_empty() -> None:
    with patch(MATCH, AsyncMock(side_effect=RuntimeError("db"))):
        audience = await resolve(client(True, ["U1"]))
    assert audience.is_public is False
    assert audience.allowed_identity_ids == []
