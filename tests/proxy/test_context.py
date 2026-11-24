import os
import time

import pytest

from checkcord.proxy.context import MAX_CONTEXT_CHARS, DynamicContextCache

pytestmark = pytest.mark.asyncio


async def test_dynamic_context_cache_lifecycle(tmp_path):
    # Setup test file relative to temp dir by changing working dir
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        # Create folder structure
        os.makedirs(".context", exist_ok=True)
        rel_path = os.path.join(".context", "ARCHITECTURE.md")

        cache = DynamicContextCache(relative_path=rel_path)

        # 1. Test missing file
        assert await cache.get_context_string() is None

        # 2. Test writing file
        with open(rel_path, "w", encoding="utf-8") as f:  # noqa: ASYNC101, ASYNC230
            f.write("Line 1\nLine 2")

        context_str = await cache.get_context_string()
        assert context_str is not None
        assert "<checkcord_context>" in context_str
        assert "Line 1\nLine 2" in context_str
        assert "</checkcord_context>" in context_str

        # 3. Test caching (no read if mtime is unchanged)
        # We modify mtime to test cache reload

        # Wait a tiny bit and overwrite to update mtime
        time.sleep(0.01)  # noqa: ASYNC101, ASYNC251
        with open(rel_path, "w", encoding="utf-8") as f:  # noqa: ASYNC101, ASYNC230
            f.write("Updated line")

        updated_context = await cache.get_context_string()
        assert "Updated line" in updated_context
        assert "Line 1" not in updated_context

    finally:
        os.chdir(old_cwd)


async def test_context_truncation(tmp_path):
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        os.makedirs(".context", exist_ok=True)
        rel_path = os.path.join(".context", "ARCHITECTURE.md")

        cache = DynamicContextCache(relative_path=rel_path)

        # Write content longer than MAX_CONTEXT_CHARS
        excess_content = "A" * (MAX_CONTEXT_CHARS + 10)
        with open(rel_path, "w", encoding="utf-8") as f:  # noqa: ASYNC101, ASYNC230
            f.write(excess_content)

        context_str = await cache.get_context_string()
        assert context_str is not None
        assert "...[TRUNCATED BY PROXY]" in context_str
        # Length without tags should be MAX_CONTEXT_CHARS + len("\n...[TRUNCATED BY PROXY]")
        inner_content = context_str.replace("<checkcord_context>\n", "").replace(
            "\n</checkcord_context>", ""
        )
        assert len(inner_content) == MAX_CONTEXT_CHARS + len("\n...[TRUNCATED BY PROXY]")

    finally:
        os.chdir(old_cwd)


async def test_context_string_is_redacted(tmp_path):
    """The regex pass runs on the file's content before it is wrapped and returned."""
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        os.makedirs(".context", exist_ok=True)
        rel_path = os.path.join(".context", "ARCHITECTURE.md")

        cache = DynamicContextCache(relative_path=rel_path)

        with open(rel_path, "w", encoding="utf-8") as f:  # noqa: ASYNC101, ASYNC230
            f.write("Contact alice@acme.example for the auth service owner.")

        context_str = await cache.get_context_string()

        assert context_str is not None
        assert "[REDACTED:EMAIL]" in context_str
        assert "alice@acme.example" not in context_str
    finally:
        os.chdir(old_cwd)
