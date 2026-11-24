from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def unsynced_directory(request: pytest.FixtureRequest) -> Iterator[None]:
    """Unit tests mock the engine, so the chat directory lookups find nothing."""
    if request.path.name in ("test_identity.py", "test_directory_sync_db.py"):
        yield
        return
    channel = AsyncMock(return_value=None)
    with (
        patch(
            "checkcord.adapters.chat.core.identity.resolve_author_identity",
            AsyncMock(return_value=(None, None, None)),
        ),
        patch("checkcord.adapters.chat.providers.slack.listeners.resolve_channel_name", channel),
        patch("checkcord.adapters.chat.providers.msteams.listeners.resolve_channel_name", channel),
    ):
        yield
