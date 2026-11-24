"""The width of the record embedding column, kept equal to EMBEDDING_DIMENSION."""

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

TABLE, COLUMN = "canonical_contexts", "embedding"
# pgvector's HNSW index takes vectors up to this width.
HNSW_MAX_DIMENSIONS = 2000


class EmbeddingColumnError(RuntimeError):
    pass


async def column_dimension(conn: AsyncConnection, table: str = TABLE) -> int | None:
    """The column's declared width; None when the table is missing or the width is unset."""
    width = await conn.scalar(
        text(
            "SELECT atttypmod FROM pg_attribute WHERE attrelid = to_regclass(:table) "
            "AND attname = :column AND NOT attisdropped"
        ),
        {"table": table, "column": COLUMN},
    )
    return int(width) if width is not None and width > 0 else None


def mismatch(column: int | None, dimension: int) -> str | None:
    if column is None or column == dimension:
        return None
    return (
        f"{TABLE}.{COLUMN} holds {column}-wide vectors but EMBEDDING_DIMENSION is {dimension}; "
        "run `checkcord migrate` to resize the column."
    )


async def sync(
    engine: AsyncEngine, dimension: int, *, reset: bool = False, table: str = TABLE
) -> str | None:
    """Resize the column to ``dimension``; returns what changed, or None if nothing did.

    Stored vectors of another width make the resize fail unless ``reset`` clears them.
    """
    async with engine.connect() as conn:
        current = await column_dimension(conn, table)
    if current is None or current == dimension:
        return None
    using = " USING NULL" if reset else ""
    index = f"ix_{table}_{COLUMN}_hnsw"
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP INDEX IF EXISTS {index}"))
            await conn.execute(
                text(f"ALTER TABLE {table} ALTER COLUMN {COLUMN} TYPE vector({dimension}){using}")
            )
            if dimension <= HNSW_MAX_DIMENSIONS:
                await conn.execute(
                    text(f"CREATE INDEX {index} ON {table} USING hnsw ({COLUMN} vector_cosine_ops)")
                )
    except DBAPIError as e:
        if "dimensions" not in str(e):
            raise
        raise EmbeddingColumnError(
            f"Stored records have {current}-wide embeddings and EMBEDDING_DIMENSION is "
            f"{dimension}. Run `checkcord migrate --reset-embeddings` to clear them, then "
            "`python scripts/reembed_canonical_contexts.py` to embed every record again."
        ) from e
    change = f"resized {table}.{COLUMN} from vector({current}) to vector({dimension})"
    if reset:
        change += "; stored embeddings cleared"
    if dimension > HNSW_MAX_DIMENSIONS:
        change += f"; no HNSW index above {HNSW_MAX_DIMENSIONS} dimensions, search scans the table"
    return change
