from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from checkcord.adapters.chat.providers.msteams.audience import resolve_msteams_audience

GRAPH_GET = "checkcord.adapters.chat.providers.msteams.audience._Graph.get"
MATCH = "checkcord.services.accounts.match"


def matching(ids: list[Any]) -> AsyncMock:
    async def match(
        engine: Any, tenant: Any, platform: str, native_ids: Any, lookup: Any = None
    ) -> Any:
        return dict(zip(native_ids, ids, strict=False)), []

    return AsyncMock(side_effect=match)


def graph(responses: dict[str, Any]) -> AsyncMock:
    async def get(path: str) -> Any:
        return responses.get(path)

    return AsyncMock(side_effect=get)


async def resolve(conversation_type: str = "channel", team_id: str | None = "T1") -> Any:
    return await resolve_msteams_audience(
        redis=None,
        db_engine=AsyncMock(),
        tenant_id=uuid4(),
        ms_tenant_id="ms-tenant",
        conversation_type=conversation_type,
        channel_id="19:abc@thread.tacv2",
        team_id=team_id,
        token="t",
    )


@pytest.mark.asyncio
async def test_standard_channel_is_public() -> None:
    responses = {"teams/T1/channels/19:abc@thread.tacv2": {"membershipType": "standard"}}
    with patch(GRAPH_GET, graph(responses)):
        audience = await resolve()
    assert audience.is_public is True


@pytest.mark.asyncio
async def test_private_channel_lists_mapped_members() -> None:
    ids = [uuid4()]
    responses = {
        "teams/T1/channels/19:abc@thread.tacv2": {"membershipType": "private"},
        "teams/T1/channels/19:abc@thread.tacv2/members": {"value": [{"userId": "aad-1"}]},
    }
    with patch(GRAPH_GET, graph(responses)), patch(MATCH, matching(ids)) as r:
        audience = await resolve()
    assert audience.is_public is False
    assert audience.allowed_identity_ids == ids
    assert list(r.call_args.args[3]) == ["aad-1"]


@pytest.mark.asyncio
async def test_chat_uses_chat_members() -> None:
    ids = [uuid4()]
    responses = {"chats/19:abc@thread.tacv2/members": {"value": [{"userId": "aad-2"}]}}
    with patch(GRAPH_GET, graph(responses)), patch(MATCH, matching(ids)):
        audience = await resolve(conversation_type="groupChat", team_id=None)
    assert audience.is_public is False
    assert audience.allowed_identity_ids == ids


@pytest.mark.asyncio
async def test_unreadable_channel_is_empty_not_public() -> None:
    with patch(GRAPH_GET, graph({})), patch(MATCH, matching([uuid4()])):
        audience = await resolve()
    assert audience.is_public is False
    assert audience.allowed_identity_ids == []
