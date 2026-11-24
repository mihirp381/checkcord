from typing import Any
from unittest.mock import AsyncMock, MagicMock


def is_rls_setting(stmt: Any) -> bool:
    return str(stmt).lstrip().upper().startswith("SET LOCAL APP.")


def data_statements(conn: AsyncMock | MagicMock) -> list[Any]:
    return [c.args[0] for c in conn.execute.call_args_list if not is_rls_setting(c.args[0])]


def table_statements(conn: AsyncMock | MagicMock, table: str) -> list[Any]:
    return [
        s for s in data_statements(conn) if getattr(getattr(s, "table", None), "name", "") == table
    ]
