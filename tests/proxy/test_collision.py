from pathlib import Path
from uuid import uuid4

import aiosqlite
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.core.config import get_settings
from checkcord.proxy.collision import detect_collisions, extract_symbols_from_payload
from checkcord.proxy.ledger import init_ledger_db
from checkcord.storage.postgres import PostgresEventStore, tenant_context

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_extract_symbols_from_payload():
    # Test OpenAI structure
    openai_payload = {
        "model": "gpt-4",
        "messages": [
            {
                "role": "system",
                "content": "You are a helpful assistant. The current time is 12:00:00.",
            },
            {
                "role": "user",
                "content": (
                    "Please rewrite the calculate_hash function or the TokenBucket class. "
                    "Ignore init or setup."
                ),
            },
        ],
    }
    symbols = extract_symbols_from_payload(openai_payload)
    assert "calculate_hash" in symbols
    assert "TokenBucket" in symbols
    assert "init" not in symbols  # Too short (<=4 chars)
    assert "setup" not in symbols  # Stopword

    # Test Anthropic structure
    anthropic_payload = {
        "model": "claude-3",
        "system": "System instructions here, referencing calculate_tax.",
        "messages": [{"role": "user", "content": "Call processPayment method."}],
    }
    symbols_anthropic = extract_symbols_from_payload(anthropic_payload)
    assert "calculate_tax" in symbols_anthropic
    assert "processPayment" in symbols_anthropic


async def test_detect_collisions(tmp_path: Path) -> None:
    # Setup SQLite ledger db in temp path
    db_path = str(tmp_path / "ledger.db")
    await init_ledger_db(db_path)

    # Pre-populate SQLite with some modified files on parallel branches
    async with aiosqlite.connect(db_path) as db:
        # Add a file modified on another branch
        await db.execute(
            "INSERT INTO active_branches (branch_name, commit_hash) VALUES (?, ?)",
            ("feature/auth", "abc12345"),
        )
        await db.execute(
            "INSERT INTO modified_files (branch_name, file_path, status) VALUES (?, ?, ?)",
            ("feature/auth", "src/auth.py", "M"),
        )
        # Add a file modified on our current branch (should not collide)
        await db.execute(
            "INSERT INTO active_branches (branch_name, commit_hash) VALUES (?, ?)",
            ("main", "def56789"),
        )
        await db.execute(
            "INSERT INTO modified_files (branch_name, file_path, status) VALUES (?, ?, ?)",
            ("main", "src/main.py", "M"),
        )
        await db.commit()

    # Setup Postgres AST Nodes
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    tenant_id = uuid4()

    async with tenant_context(engine, tenant_id) as conn:
        store = PostgresEventStore(conn)
        # Insert AST nodes using an absolute path to verify normalization
        await store.upsert_ast_node_and_check_drift(
            tenant_id=tenant_id,
            repo_identifier="github.com/org/repo",
            file_path="/Users/alex/workspace/repo/src/auth.py",
            symbol_name="calculate_hash",
            node_type="function",
            merkle_hash="hash1",
        )
        await store.upsert_ast_node_and_check_drift(
            tenant_id=tenant_id,
            repo_identifier="github.com/org/repo",
            file_path="src/main.py",
            symbol_name="start_server",
            node_type="function",
            merkle_hash="hash2",
        )
        expected_node_id = (await store.find_ast_nodes_by_symbols(tenant_id, ["calculate_hash"]))[
            0
        ]["id"]

    # Test detection: we ask about calculate_hash (collides because modified on feature/auth)
    collisions = await detect_collisions(
        symbols={"calculate_hash"},
        current_branch="main",
        tenant_id=tenant_id,
        pg_engine=engine,
        ledger_db_path=db_path,
    )
    assert len(collisions) == 1
    assert collisions[0]["symbol_name"] == "calculate_hash"
    assert collisions[0]["file_path"] == "src/auth.py"
    assert collisions[0]["branch_name"] == "feature/auth"
    # ast_node_id/repo_identifier are propagated so a viewer link can be
    # built for the colliding symbol.
    assert collisions[0]["ast_node_id"] == str(expected_node_id)
    assert collisions[0]["repo_identifier"] == "github.com/org/repo"

    # Test detection: we ask about start_server (modified on main, which is
    # current_branch, so no collision)
    collisions_main = await detect_collisions(
        symbols={"start_server"},
        current_branch="main",
        tenant_id=tenant_id,
        pg_engine=engine,
        ledger_db_path=db_path,
    )
    assert len(collisions_main) == 0

    # Cleanup Postgres
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(text("DELETE FROM ast_nodes"))
    await engine.dispose()
