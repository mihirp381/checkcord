from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from checkcord.models.common import AccessControl
from checkcord.services import repo_audience
from checkcord.services.repo_audience import RepoAudience

A, B = str(uuid4()), str(uuid4())


def test_public_record_is_allowed_anywhere() -> None:
    assert RepoAudience().allows({"is_public": True})


def test_unknown_readers_allow_only_public_records() -> None:
    assert not RepoAudience().allows({"is_public": False, "allowed_identity_ids": [A]})


def test_every_reader_must_be_in_the_audience() -> None:
    audience = RepoAudience(frozenset({A, B}))
    assert audience.allows({"is_public": False, "allowed_identity_ids": [A, B]})
    assert not audience.allows({"is_public": False, "allowed_identity_ids": [A]})


def test_admin_overrides_count_as_audience() -> None:
    audience = RepoAudience(frozenset({A, B}))
    assert audience.allows(
        {"is_public": False, "allowed_identity_ids": [A], "admin_overrides": [B]}
    )


async def _readable(tokens: dict[str, str | None], audience: Any, who: UUID) -> set[str]:
    async def token(engine: Any, tenant: Any, repo: str) -> str | None:
        return tokens[repo]

    with (
        patch("checkcord.services.github_tokens.installation_token", token),
        patch("checkcord.services.audiences.current", audience),
    ):
        return await repo_audience.readable_repositories(MagicMock(), uuid4(), who, list(tokens))


@pytest.mark.asyncio
async def test_readable_repositories_follow_the_github_audience() -> None:
    reader, other = uuid4(), uuid4()
    private = AsyncMock(return_value=AccessControl(allowed_identity_ids=[reader]))
    tokens = {"acme/pay": "t", "acme/push": None}
    assert await _readable(tokens, private, reader) == {"acme/pay", "acme/push"}
    assert await _readable(tokens, private, other) == {"acme/push"}
    public = AsyncMock(return_value=AccessControl(is_public=True))
    assert await _readable(tokens, public, other) == {"acme/pay", "acme/push"}
    failing = AsyncMock(side_effect=RuntimeError("down"))
    assert await _readable(tokens, failing, reader) == {"acme/push"}
