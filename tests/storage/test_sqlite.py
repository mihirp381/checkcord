import os
import tempfile

import pytest

from checkcord.storage.sqlite import SQLiteTelemetryStore

pytestmark = pytest.mark.asyncio


async def test_sqlite_telemetry_store_lifecycle() -> None:
    # Use a temporary file for the database
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        store = SQLiteTelemetryStore(db_path)
        await store.init_db()

        # Check initial empty summary
        summary = await store.get_summary()
        assert summary["total_requests"] == 0
        assert summary["total_prompt"] == 0
        assert summary["total_completion"] == 0
        assert summary["total_cache_creation"] == 0
        assert summary["total_cache_read"] == 0

        # Log a request
        await store.log_request(
            provider="openai",
            model="gpt-4o",
            prompt_tokens=1000,
            completion_tokens=200,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=500,
        )

        # Log another request
        await store.log_request(
            provider="anthropic",
            model="claude-3-5-sonnet",
            prompt_tokens=2000,
            completion_tokens=400,
            cache_creation_input_tokens=1000,
            cache_read_input_tokens=500,
        )

        # Validate summary
        summary = await store.get_summary()
        assert summary["total_requests"] == 2
        assert summary["total_prompt"] == 3000
        assert summary["total_completion"] == 600
        assert summary["total_cache_creation"] == 1000
        assert summary["total_cache_read"] == 1000

        # Validate getting all logs
        logs = await store.get_all_logs()
        assert len(logs) == 2
        assert logs[0]["provider"] == "openai"
        assert logs[0]["model"] == "gpt-4o"
        assert logs[0]["prompt_tokens"] == 1000
        assert logs[0]["completion_tokens"] == 200
        assert logs[0]["cache_creation_input_tokens"] == 0
        assert logs[0]["cache_read_input_tokens"] == 500

        assert logs[1]["provider"] == "anthropic"
        assert logs[1]["model"] == "claude-3-5-sonnet"
        assert logs[1]["prompt_tokens"] == 2000
        assert logs[1]["completion_tokens"] == 400
        assert logs[1]["cache_creation_input_tokens"] == 1000
        assert logs[1]["cache_read_input_tokens"] == 500

    finally:
        if os.path.exists(db_path):  # noqa: ASYNC240
            os.remove(db_path)
