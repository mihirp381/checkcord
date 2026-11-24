import json
from unittest.mock import patch

import pytest

from checkcord.proxy import mcp_server

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("missing", ["CHECKCORD_PROXY_API_URL", "CHECKCORD_PROXY_API_KEY"])
async def test_call_remote_tool_without_remote_config_returns_error(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setenv("CHECKCORD_PROXY_API_URL", "http://checkcord.test")
    monkeypatch.setenv("CHECKCORD_PROXY_API_KEY", "chk_test")
    monkeypatch.delenv(missing)
    with patch.object(mcp_server, "_remote_http_client") as client:
        data = json.loads(await mcp_server.call_remote_tool("view_active_decisions", {}))
    assert "CHECKCORD_PROXY_API_URL" in data["error"]
    client.assert_not_called()
