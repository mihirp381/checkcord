import os
import sqlite3
from typing import Any

import aiosqlite
import structlog

logger = structlog.get_logger(__name__)


# No RLS: a local single-user file that can be reset at any time.
class SQLiteTelemetryStore:
    """Proxy telemetry in a local SQLite file."""

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)

    async def init_db(self) -> None:
        """Initialize the telemetry_logs table in WAL mode."""
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        # Initialize synchronously to prevent race conditions during server startup
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS telemetry_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_input_tokens INTEGER NOT NULL DEFAULT 0
                );
            """)
            conn.commit()
            logger.info("telemetry_database_initialized", path=self.db_path)
        except Exception as e:
            logger.error("failed_to_initialize_telemetry_db", path=self.db_path, error=str(e))
            raise e
        finally:
            conn.close()

    async def log_request(
        self,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cache_creation_input_tokens: int,
        cache_read_input_tokens: int,
    ) -> None:
        """Log LLM token usage details to SQLite database."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA journal_mode=WAL;")
                await db.execute("PRAGMA synchronous=NORMAL;")
                await db.execute(
                    """
                    INSERT INTO telemetry_logs (
                        provider, model, prompt_tokens, completion_tokens,
                        cache_creation_input_tokens, cache_read_input_tokens
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        provider,
                        model,
                        prompt_tokens,
                        completion_tokens,
                        cache_creation_input_tokens,
                        cache_read_input_tokens,
                    ),
                )
                await db.commit()
        except Exception as e:
            logger.error("failed_to_log_telemetry", path=self.db_path, error=str(e))

    async def get_summary(self) -> dict[str, Any]:
        """Fetch consolidated token metrics from the database."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA journal_mode=WAL;")
                await db.execute("PRAGMA synchronous=NORMAL;")
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    """
                    SELECT
                        COUNT(*) as total_requests,
                        COALESCE(SUM(prompt_tokens), 0) as total_prompt,
                        COALESCE(SUM(completion_tokens), 0) as total_completion,
                        COALESCE(SUM(cache_creation_input_tokens), 0) as total_cache_creation,
                        COALESCE(SUM(cache_read_input_tokens), 0) as total_cache_read
                    FROM telemetry_logs
                    """
                ) as cursor:
                    row = await cursor.fetchone()
                    return (
                        dict(row)
                        if row
                        else {
                            "total_requests": 0,
                            "total_prompt": 0,
                            "total_completion": 0,
                            "total_cache_creation": 0,
                            "total_cache_read": 0,
                        }
                    )
        except Exception as e:
            logger.error("failed_to_fetch_telemetry_summary", path=self.db_path, error=str(e))
            return {
                "total_requests": 0,
                "total_prompt": 0,
                "total_completion": 0,
                "total_cache_creation": 0,
                "total_cache_read": 0,
            }

    async def get_all_logs(self) -> list[dict[str, Any]]:
        """Fetch all individual logs from the database."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA journal_mode=WAL;")
                await db.execute("PRAGMA synchronous=NORMAL;")
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT provider, model, prompt_tokens, completion_tokens, "
                    "cache_creation_input_tokens, cache_read_input_tokens FROM telemetry_logs"
                ) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(r) for r in rows]
        except Exception as e:
            logger.error("failed_to_fetch_telemetry_logs", path=self.db_path, error=str(e))
            return []
