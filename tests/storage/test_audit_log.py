import json
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from starlette.requests import Request

from checkcord.api import deps
from checkcord.core.config import get_settings
from checkcord.storage import audit


def _audit_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "checkcord.audit"]


def _inserted(conn: AsyncMock) -> dict[str, object]:
    stmt = conn.execute.call_args.args[0]
    params: dict[str, object] = stmt.compile(dialect=postgresql.dialect()).params
    return params


@pytest.mark.asyncio
async def test_record_writes_one_row_and_one_line(caplog: pytest.LogCaptureFixture) -> None:
    conn = AsyncMock()
    tenant, key, user, ctx = uuid4(), uuid4(), uuid4(), uuid4()
    actor = audit.Actor(
        channel=audit.MCP, key_id=key, key_name="laptop", user_id=user, user_email="a@x.test"
    )
    with caplog.at_level(logging.INFO, logger="checkcord.audit"):
        await audit.record(
            conn,
            tenant,
            actor,
            "mcp.view_active_decisions",
            params={"repo": "acme/api", "team": None},
            record_ids=[ctx, ctx, None],
            redacted=1,
            result_count=1,
        )

    conn.execute.assert_awaited_once()
    row = _inserted(conn)
    assert row["tenant_id"] == tenant and row["key_id"] == key and row["user_id"] == user
    assert row["params"] == {"repo": "acme/api"}
    assert row["record_ids"] == [str(ctx)]
    [line] = _audit_lines(caplog)
    assert line["logger"] == "checkcord.audit"
    assert line["action"] == "mcp.view_active_decisions"
    assert line["user_email"] == "a@x.test" and line["key_name"] == "laptop"
    assert line["record_ids"] == [str(ctx)] and line["redacted_count"] == 1
    assert "summary" not in line and "content" not in json.dumps(line)


@pytest.mark.asyncio
async def test_record_failure_propagates() -> None:
    conn = AsyncMock()
    conn.execute.side_effect = RuntimeError("insert refused")
    with pytest.raises(RuntimeError, match="insert refused"):
        await audit.record(conn, uuid4(), audit.CLI_ACTOR, "key.created")


@pytest.mark.asyncio
async def test_lines_can_be_turned_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("AUDIT_LOG_LINES", "false")
    get_settings.cache_clear()
    with caplog.at_level(logging.INFO, logger="checkcord.audit"):
        await audit.record(AsyncMock(), uuid4(), audit.CLI_ACTOR, "key.created")
    assert _audit_lines(caplog) == []


def test_search_text_is_truncated() -> None:
    assert audit.search_text("x" * 500) == "x" * audit.SEARCH_TEXT_LIMIT
    assert audit.search_text(None) is None


def test_cursor_round_trip_and_garbage() -> None:
    row = {"created_at": datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=UTC), "id": uuid4()}
    assert audit.decode_cursor(audit.encode_cursor(row)) == (row["created_at"], row["id"])
    for bad in ("", "not-base64!", audit.encode_cursor(row)[:-4]):
        with pytest.raises(ValueError, match="invalid cursor"):
            audit.decode_cursor(bad)


def test_filters_compile_to_where_clauses() -> None:
    tenant, user = uuid4(), uuid4()
    stmt = audit._filtered(
        tenant,
        audit.Filters(
            since=datetime(2026, 1, 1),
            user=str(user),
            key="laptop",
            action="graph.node",
            channel="api",
            record_id="abc",
        ),
    )
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    for clause in (
        "created_at >=",
        "user_id =",
        "key_name =",
        "action =",
        "channel =",
        "record_ids @>",
    ):
        assert clause in sql
    by_email = str(audit._filtered(tenant, audit.Filters(user="A@X.test")).compile())
    assert "lower(audit_log_entries.user_email)" in by_email


def test_csv_row_flattens_params_and_ids() -> None:
    row = {name: None for name in audit.COLUMNS}
    row.update(
        id=uuid4(),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        params={"b": 1, "a": "x"},
        record_ids=["r1", "r2"],
    )
    out = dict(zip(audit.COLUMNS, audit.csv_row(row), strict=True))
    assert out["params"] == '{"a": "x", "b": 1}'
    assert out["record_ids"] == "r1 r2"
    assert out["user_email"] == ""


def _request(path: str, host: str = "10.0.0.1") -> Request:
    return Request({"type": "http", "path": path, "headers": [], "client": (host, 1234)})


def test_channel_for_path() -> None:
    assert deps.channel_for(_request("/mcp")) == audit.MCP
    assert deps.channel_for(_request("/scim/v2/Users")) == audit.SCIM
    assert deps.channel_for(_request("/api/v1/graph/node/x")) == audit.API


@pytest.mark.asyncio
async def test_auth_failures_are_recorded_once_per_key_prefix_and_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = uuid4()
    monkeypatch.setenv("DEFAULT_TENANT_ID", str(tenant))
    get_settings.cache_clear()
    record = AsyncMock()
    monkeypatch.setattr(audit, "record", record)
    conn = AsyncMock()
    conn.execute.return_value = MagicMock(first=MagicMock(return_value=None))
    begin = MagicMock()
    begin.__aenter__ = AsyncMock(return_value=conn)
    begin.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock(begin=MagicMock(return_value=begin))
    token = "chk_" + uuid4().hex

    for _ in range(3):
        await deps.record_auth_failure(_request("/mcp"), token, engine)
    await deps.record_auth_failure(_request("/mcp", host="10.0.0.2"), token, engine)

    assert record.await_count == 2
    args, kwargs = record.call_args
    assert args[1] == tenant and args[3] == "auth.failed"
    assert kwargs["params"]["key_prefix"] == token[:8]
    assert kwargs["params"]["reason"] == "unknown key"
    assert token not in json.dumps(kwargs["params"])


def test_auth_failures_have_a_per_minute_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "AUTH_FAILURES_PER_MINUTE", 2)
    assert deps._first_failure_this_minute("chk_aaaa", "h1")
    assert deps._first_failure_this_minute("chk_bbbb", "h1")
    assert not deps._first_failure_this_minute("chk_cccc", "h1")
    assert not deps._first_failure_this_minute("chk_aaaa", "h1")
