from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def trusted_bot_framework(request: pytest.FixtureRequest) -> Iterator[None]:
    """Activity tests exercise routing; token checks live in test_auth.py."""
    if request.node.get_closest_marker("real_bot_auth"):
        yield
        return
    with patch(
        "checkcord.adapters.chat.providers.msteams.listeners.verify_bot_token",
        AsyncMock(return_value={}),
    ):
        yield
