import asyncio
import os
import sqlite3
import subprocess
from types import SimpleNamespace

import aiosqlite
import pytest

from checkcord.proxy.ledger import (
    GitWatcher,
    GitWorkspaceEventHandler,
    init_ledger_db,
    parse_git_status,
    parse_worktrees,
)


def test_parse_worktrees():
    sample = (
        "worktree /path/to/repo\n"
        "HEAD aaa70d1f0445bf76038a8c29f8e6d2e6b8ef8623\n"
        "branch refs/heads/dev\n"
        "\n"
        "worktree /path/to/worktree2\n"
        "HEAD bbb80d1f0445bf76038a8c29f8e6d2e6b8ef8624\n"
        "branch refs/heads/feature/x\n"
    )
    wts = parse_worktrees(sample)
    assert len(wts) == 2
    assert wts[0] == {
        "path": "/path/to/repo",
        "commit": "aaa70d1f0445bf76038a8c29f8e6d2e6b8ef8623",
        "branch": "dev",
    }
    assert wts[1] == {
        "path": "/path/to/worktree2",
        "commit": "bbb80d1f0445bf76038a8c29f8e6d2e6b8ef8624",
        "branch": "feature/x",
    }


def test_parse_worktrees_detached():
    sample = "worktree /path/to/detached\nHEAD ccc90d1f0445bf76038a8c29f8e6d2e6b8ef8625\n"
    wts = parse_worktrees(sample)
    assert len(wts) == 1
    assert wts[0] == {
        "path": "/path/to/detached",
        "commit": "ccc90d1f0445bf76038a8c29f8e6d2e6b8ef8625",
        "branch": "",
    }


def test_parse_git_status():
    sample = (
        " M src/checkcord/proxy/server.py\n?? tests/proxy/test_ledger.py\n R old.py -> new.py\n"
    )
    status = parse_git_status(sample)
    assert len(status) == 3
    assert status[0] == ("src/checkcord/proxy/server.py", "M")
    assert status[1] == ("tests/proxy/test_ledger.py", "??")
    assert status[2] == ("new.py", "R")


@pytest.mark.asyncio
async def test_init_ledger_db(tmp_path):
    db_file = os.path.join(tmp_path, "ledger.db")
    await init_ledger_db(db_file)

    assert os.path.exists(db_file)  # noqa: ASYNC240

    # Verify tables and WAL mode
    conn = sqlite3.connect(db_file)
    try:
        # Check WAL mode
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode;")
        mode = cursor.fetchone()[0]
        assert mode.lower() == "wal"

        # Check tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cursor.fetchall()]
        assert "active_branches" in tables
        assert "modified_files" in tables
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_git_watcher_updates_db(tmp_path):
    db_file = os.path.join(tmp_path, "ledger.db")
    workspace = os.path.join(tmp_path, "workspace")
    os.makedirs(workspace, exist_ok=True)

    # Create a mock .git directory to pass the check
    os.makedirs(os.path.join(workspace, ".git"), exist_ok=True)

    watcher = GitWatcher(workspace_path=workspace, db_path=db_file)
    watcher._is_git_repo = True

    # Mock _run_git calls
    # For worktree list
    async def mock_run_git_wt(args, cwd):
        if "worktree" in args:
            return (
                f"worktree {workspace}\n"
                "HEAD aaa70d1f0445bf76038a8c29f8e6d2e6b8ef8623\n"
                "branch refs/heads/dev\n"
            )
        elif "status" in args:
            return " M file1.py\n?? file2.py\n"
        return ""

    watcher._run_git = mock_run_git_wt

    await init_ledger_db(db_file)
    await watcher.update_state()

    # Query database and verify
    async with aiosqlite.connect(db_file) as db:
        async with db.execute("SELECT branch_name, commit_hash FROM active_branches") as cursor:
            branches = list(await cursor.fetchall())
            assert len(branches) == 1
            assert branches[0] == ("dev", "aaa70d1f0445bf76038a8c29f8e6d2e6b8ef8623")

        async with db.execute(
            "SELECT file_path, status FROM modified_files WHERE branch_name='dev'"
        ) as cursor:
            files = list(await cursor.fetchall())
            assert len(files) == 2
            assert ("file1.py", "M") in files
            assert ("file2.py", "??") in files


@pytest.mark.asyncio
async def test_git_watcher_pruning(tmp_path):
    db_file = os.path.join(tmp_path, "ledger.db")
    workspace = os.path.join(tmp_path, "workspace")
    os.makedirs(workspace, exist_ok=True)
    os.makedirs(os.path.join(workspace, ".git"), exist_ok=True)

    # Manually populate DB with old branch data that is no longer active
    await init_ledger_db(db_file)
    async with aiosqlite.connect(db_file) as db:
        await db.execute(
            "INSERT INTO active_branches (branch_name, commit_hash) VALUES (?, ?)",
            ("old-branch", "deadbeef"),
        )
        await db.execute(
            "INSERT INTO modified_files (branch_name, file_path, status) VALUES (?, ?, ?)",
            ("old-branch", "old_file.py", "M"),
        )
        await db.commit()

    watcher = GitWatcher(workspace_path=workspace, db_path=db_file)
    watcher._is_git_repo = True

    async def mock_run_git_wt(args, cwd):
        if "worktree" in args:
            return (
                f"worktree {workspace}\n"
                "HEAD aaa70d1f0445bf76038a8c29f8e6d2e6b8ef8623\n"
                "branch refs/heads/dev\n"
            )
        elif "status" in args:
            return ""
        return ""

    watcher._run_git = mock_run_git_wt
    await watcher.update_state()

    # Query database and verify "old-branch" has been pruned
    async with aiosqlite.connect(db_file) as db:
        async with db.execute("SELECT branch_name FROM active_branches") as cursor:
            branches = [row[0] for row in await cursor.fetchall()]
            assert "old-branch" not in branches
            assert "dev" in branches

        async with db.execute("SELECT file_path FROM modified_files") as cursor:
            files = [row[0] for row in await cursor.fetchall()]
            assert "old_file.py" not in files


class _Loop:
    def __init__(self) -> None:
        self.calls = 0

    def call_soon_threadsafe(self, _cb) -> None:
        self.calls += 1


@pytest.mark.parametrize(
    ("event_type", "path", "triggers"),
    [
        ("opened", "/ws/.git/HEAD", False),
        ("closed_no_write", "/ws/.git/index", False),
        ("opened", "/ws/file.txt", False),
        ("modified", "/ws/file.txt", True),
        ("closed", "/ws/file.txt", True),
        ("modified", "/ws/.git/index", True),
        ("modified", "/ws/.git/objects/ab/cd", False),
    ],
)
def test_handler_ignores_reads(event_type, path, triggers):
    loop = _Loop()
    handler = GitWorkspaceEventHandler(loop, lambda: None, {".context"}, "/ws/.git")
    handler.on_any_event(SimpleNamespace(is_directory=False, event_type=event_type, src_path=path))
    assert (loop.calls == 1) is triggers


def _init_repo(workspace: str) -> str:
    os.makedirs(workspace, exist_ok=True)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=workspace, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@test.example")
    with open(os.path.join(workspace, "file.txt"), "w") as f:
        f.write("initial content")
    git("add", "file.txt")
    git("commit", "-m", "initial commit")
    return git("rev-parse", "--abbrev-ref", "HEAD")


def _write(path: str, text: str) -> None:
    with open(path, "w") as f:
        f.write(text)


async def _modified_files(db_file: str) -> list:
    async with aiosqlite.connect(db_file) as db:
        async with db.execute("SELECT file_path, status FROM modified_files") as cursor:
            return list(await cursor.fetchall())


@pytest.mark.asyncio
async def test_git_watcher_integration(tmp_path):
    workspace = os.path.join(tmp_path, "workspace")
    current_branch = _init_repo(workspace)
    db_file = os.path.join(workspace, ".context", "ledger.db")
    watcher = GitWatcher(workspace_path=workspace, db_path=db_file)
    await watcher.start()
    try:
        async with aiosqlite.connect(db_file) as db:
            async with db.execute("SELECT branch_name FROM active_branches") as cursor:
                branches = [row[0] for row in await cursor.fetchall()]
                assert current_branch in branches

        _write(os.path.join(workspace, "file.txt"), "modified content")

        files: list = []
        for _ in range(100):
            files = await _modified_files(db_file)
            if files:
                break
            await asyncio.sleep(0.1)
        assert files == [("file.txt", "M")]
    finally:
        await watcher.stop()
