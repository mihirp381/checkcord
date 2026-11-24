"""Resizing the embedding column to EMBEDDING_DIMENSION."""

from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.core.config import get_settings
from checkcord.storage import vector_column

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture
async def table() -> AsyncGenerator[tuple[AsyncEngine, str], None]:
    name = f"vec_{uuid4().hex[:10]}"
    engine = create_async_engine(get_settings().database_url)
    async with engine.begin() as conn:
        await conn.execute(text(f"CREATE TABLE {name} (id serial, embedding vector(4))"))
    try:
        yield engine, name
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE {name}"))
        await engine.dispose()


async def _width(engine: AsyncEngine, name: str) -> int | None:
    async with engine.connect() as conn:
        return await vector_column.column_dimension(conn, name)


async def _index(engine: AsyncEngine, name: str) -> bool:
    async with engine.connect() as conn:
        found = await conn.scalar(
            text("SELECT to_regclass(:i)"), {"i": f"ix_{name}_embedding_hnsw"}
        )
    return found is not None


async def test_the_records_table_matches_the_default_setting() -> None:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            assert await vector_column.column_dimension(conn) == 1024
    finally:
        await engine.dispose()


async def test_missing_table_has_no_width(table: tuple[AsyncEngine, str]) -> None:
    engine, _ = table
    assert await _width(engine, "no_such_table") is None
    assert await vector_column.sync(engine, 8, table="no_such_table") is None


async def test_empty_column_is_resized_and_indexed(table: tuple[AsyncEngine, str]) -> None:
    engine, name = table
    assert await vector_column.sync(engine, 4, table=name) is None
    change = await vector_column.sync(engine, 8, table=name)
    assert change == f"resized {name}.embedding from vector(4) to vector(8)"
    assert await _width(engine, name) == 8
    assert await _index(engine, name)


async def test_stored_vectors_block_the_resize_until_reset(table: tuple[AsyncEngine, str]) -> None:
    engine, name = table
    async with engine.begin() as conn:
        await conn.execute(text(f"INSERT INTO {name} (embedding) VALUES ('[1,0,0,0]')"))

    with pytest.raises(vector_column.EmbeddingColumnError, match="--reset-embeddings"):
        await vector_column.sync(engine, 8, table=name)
    assert await _width(engine, name) == 4

    change = await vector_column.sync(engine, 8, reset=True, table=name)
    assert change is not None and change.endswith("stored embeddings cleared")
    async with engine.connect() as conn:
        assert await conn.scalar(text(f"SELECT count(embedding) FROM {name}")) == 0


async def test_wide_vectors_skip_the_index(table: tuple[AsyncEngine, str]) -> None:
    engine, name = table
    change = await vector_column.sync(engine, 3072, table=name)
    assert change is not None and "no HNSW index" in change
    assert not await _index(engine, name)
