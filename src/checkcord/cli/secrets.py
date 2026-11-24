from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord import connectors
from checkcord.core.crypto import encrypt_secret, is_encrypted
from checkcord.storage.postgres import app_bypass_rls_context

SECRET_COLUMNS: list[tuple[Any, tuple[str, ...]]] = [
    (c.table, c.secret_columns)
    for c in connectors.connectors()
    if c.table is not None and c.secret_columns
]
SEALED_JSON: dict[Any, tuple[tuple[str, Any], ...]] = {
    c.table: c.sealed_json for c in connectors.connectors() if c.sealed_json
}


@dataclass(frozen=True)
class TableResult:
    table: str
    rows: int
    encrypted: int


async def encrypt_stored_secrets(engine: AsyncEngine) -> list[TableResult]:
    results: list[TableResult] = []
    async with app_bypass_rls_context(engine) as conn:
        for model, columns in SECRET_COLUMNS:
            sealed_json = SEALED_JSON.get(model, ())
            selected = [model.id, *(getattr(model, c) for c in (*columns, *dict(sealed_json)))]
            rows = (await conn.execute(select(*selected).with_for_update())).mappings().all()
            changed = 0
            for row in rows:
                values: dict[str, Any] = {
                    c: encrypt_secret(row[c]) for c in columns if not is_encrypted(row[c])
                }
                for column, seal in sealed_json:
                    info = row[column] or {}
                    sealed = seal(info)
                    if sealed != info:
                        values[column] = sealed
                if values:
                    await conn.execute(update(model).where(model.id == row["id"]).values(**values))
                    changed += 1
            results.append(TableResult(model.__tablename__, len(rows), changed))
    return results
