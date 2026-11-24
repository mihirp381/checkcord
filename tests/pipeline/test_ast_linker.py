from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.pipeline.ast_linker import (
    ASTLinkerWorker,
    extract_code_symbols,
    is_camel_case,
    is_snake_case,
)

pytestmark = pytest.mark.asyncio


async def test_is_camel_case() -> None:
    assert is_camel_case("ASTNode") is True
    assert is_camel_case("astLinker") is True
    assert is_camel_case("PostgresEventStore") is True
    assert is_camel_case("Simple") is False
    assert is_camel_case("simple") is False
    assert is_camel_case("API") is False
    assert is_camel_case("A") is False
    assert is_camel_case("get_event") is False
    assert is_camel_case("someOtherSymbol123") is True


async def test_is_snake_case() -> None:
    assert is_snake_case("get_event") is True
    assert is_snake_case("get_event_by_id") is True
    assert is_snake_case("_private_func") is True
    assert is_snake_case("__init__") is True
    assert is_snake_case("simple") is False
    assert is_snake_case("_") is False
    assert is_snake_case("___") is False
    assert is_snake_case("get-event") is False
    assert is_snake_case("getEvent") is False


async def test_extract_code_symbols() -> None:
    text = (
        "We need to implement a parser in ASTNode and use get_event "
        "function, but not Simple, XML, or standard words."
    )
    expected = {"ASTNode", "get_event"}
    assert extract_code_symbols(text) == expected


async def test_extract_dotted_backticked_and_file_names() -> None:
    assert extract_code_symbols("Call ledger.post_entry first.") == {"post_entry"}
    assert extract_code_symbols("LedgerClient.post_entry, e.g. this") == {
        "post_entry",
        "LedgerClient",
    }
    assert extract_code_symbols("Use `charges` and `refund()` here.") == {"charges", "refund"}
    assert extract_code_symbols("Edit charges.py and web/app.tsx only.") == set()


def _event(text: str, source_id: str) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=uuid4(),
        tenant_id=uuid4(),
        source_platform=SourcePlatform.CUSTOM,
        source_type=SourceType.CUSTOM_PAYLOAD,
        source_id=source_id,
        happened_at="2026-07-02T12:00:00Z",
        author={"source_id": "system"},
        content_raw=text,
        content_clean=text,
        access_control={"is_public": True},
    )


async def test_ast_linker_worker_links_record_text() -> None:
    context_id = uuid4()
    event = _event("Updated method in ASTNode using get_event", str(context_id))
    with (
        patch("checkcord.pipeline.ast_linker.tenant_context") as mock_tenant_context,
        patch("checkcord.pipeline.ast_linker.link_record", AsyncMock(return_value=2)) as link,
    ):
        conn = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = conn
        mock_tenant_context.return_value = mock_ctx
        await ASTLinkerWorker(bus=AsyncMock(), engine=MagicMock()).process_message(event)
    link.assert_awaited_once_with(conn, event.tenant_id, context_id, event.content_clean)


async def test_ast_linker_worker_no_symbols() -> None:
    event = _event("No special symbols in this text.", str(uuid4()))
    with (
        patch("checkcord.pipeline.ast_linker.tenant_context") as mock_tenant_context,
        patch("checkcord.pipeline.ast_linker.link_record", AsyncMock()) as link,
    ):
        await ASTLinkerWorker(bus=AsyncMock(), engine=MagicMock()).process_message(event)
    mock_tenant_context.assert_not_called()
    link.assert_not_called()


async def test_ast_linker_worker_invalid_uuid() -> None:
    event = _event("Symbol ASTNode", "invalid-uuid-string")
    with (
        patch("checkcord.pipeline.ast_linker.tenant_context") as mock_tenant_context,
        patch("checkcord.pipeline.ast_linker.link_record", AsyncMock()) as link,
    ):
        await ASTLinkerWorker(bus=AsyncMock(), engine=MagicMock()).process_message(event)
    mock_tenant_context.assert_not_called()
    link.assert_not_called()
