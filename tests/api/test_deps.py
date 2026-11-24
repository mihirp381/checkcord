import uuid

import pytest

from checkcord.api.deps import resolve_principal
from checkcord.core.config import get_settings


@pytest.mark.asyncio
async def test_environment_tokens_are_not_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCIM_TENANT_TOKENS", f'{{"env_token_123": "{uuid.uuid4()}"}}')
    get_settings.cache_clear()
    assert not hasattr(get_settings(), "scim_tenant_tokens")
    assert await resolve_principal("env_token_123", db_engine=None) is None
