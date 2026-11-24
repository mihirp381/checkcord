import asyncio
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiosqlite
import structlog
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

logger = structlog.get_logger(__name__)


async def init_ledger_db(db_path: str) -> None:
    """Initialize the SQLite database with WAL mode and set up schemas."""
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    # Use standard sqlite3 for synchronous initialization
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS active_branches (
                branch_name TEXT PRIMARY KEY,
                commit_hash TEXT NOT NULL,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS modified_files (
                branch_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                status TEXT NOT NULL,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (branch_name, file_path)
            );
        """)
        conn.commit()
        logger.info("ledger_database_initialized", path=db_path)
    except Exception as e:
        logger.error("failed_to_initialize_ledger_db", path=db_path, error=str(e))
        raise e
    finally:
        conn.close()


def parse_worktrees(output: str) -> list[dict]:
    """Parse output of git worktree list --porcelain."""
    worktrees = []
    current: dict[str, str] = {}
    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
            if current:
                worktrees.append(current)
                current = {}
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2:
            key, val = parts
            if key == "worktree":
                if current:
                    worktrees.append(current)
                current = {"path": val, "commit": "", "branch": ""}
            elif key == "HEAD":
                current["commit"] = val
            elif key == "branch":
                branch = val
                if branch.startswith("refs/heads/"):
                    branch = branch[len("refs/heads/") :]
                current["branch"] = branch
    if current:
        worktrees.append(current)
    return worktrees


def parse_git_status(output: str) -> list[tuple[str, str]]:
    """Parse output of git status --porcelain."""
    modified = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        status = line[:2].strip()
        file_path = line[3:].strip()
        if " -> " in file_path:
            file_path = file_path.split(" -> ")[-1].strip()
        if file_path.startswith('"') and file_path.endswith('"'):
            file_path = file_path[1:-1]
        modified.append((file_path, status))
    return modified


class GitWorkspaceEventHandler(FileSystemEventHandler):
    """Reacts to changes of files git does not ignore."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        trigger_callback: Callable[[], None],
        ignored_dirs: set[str],
        git_dir: str,
    ) -> None:
        self.loop = loop
        self.trigger_callback = trigger_callback
        self.ignored_dirs = ignored_dirs
        self.git_dir = os.path.abspath(git_dir)

    def on_any_event(self, event: Any) -> None:
        # Reads (Linux reports opens) include the watcher's own git calls.
        if event.is_directory or event.event_type in ("opened", "closed_no_write"):
            return

        src_path = Path(event.src_path)
        src_path_abs = os.path.abspath(src_path)

        # Check if inside git directory
        if src_path_abs.startswith(self.git_dir):
            # Only trigger on HEAD/index updates or refs changes
            if src_path.name in ("HEAD", "index") or "refs" in src_path.parts:
                self.loop.call_soon_threadsafe(self.trigger_callback)
            return

        # Check if the path is in any other ignored directory
        path_parts = src_path.parts
        for ignored in self.ignored_dirs:
            if ignored in path_parts:
                return

        # Check destination path for moved/renamed events
        if event.event_type == "moved" and hasattr(event, "dest_path"):
            dest_parts = Path(event.dest_path).parts
            for ignored in self.ignored_dirs:
                if ignored in dest_parts:
                    return

        self.loop.call_soon_threadsafe(self.trigger_callback)


class GitWatcher:
    """Watches git repositories and worktrees and updates the SQLite ledger."""

    def __init__(self, workspace_path: str, db_path: str):
        self.workspace_path = os.path.abspath(workspace_path)
        self.db_path = os.path.abspath(db_path)
        self.observer: BaseObserver | None = None
        self._debounce_task: asyncio.Task | None = None
        self._ignored_dirs = {
            "node_modules",
            "venv",
            ".venv",
            "env",
            "target",
            "build",
            "dist",
            ".idea",
            ".vscode",
            "__pycache__",
            ".context",
        }
        self._is_git_repo = False
        self.git_dir = ""
        self.current_branch = ""

    async def start(self) -> None:
        """Start the database initialization and filesystem observer."""
        # Query absolute git directory to verify if it's a valid repository/worktree
        git_dir_output = await self._run_git(
            ["rev-parse", "--absolute-git-dir"], self.workspace_path
        )
        if not git_dir_output:
            logger.warning("workspace_not_a_git_repository", path=self.workspace_path)
            self._is_git_repo = False
            return

        self._is_git_repo = True
        self.git_dir = os.path.abspath(git_dir_output.strip())  # noqa: ASYNC240
        await init_ledger_db(self.db_path)
        await self.update_state()

        loop = asyncio.get_running_loop()
        event_handler = GitWorkspaceEventHandler(
            loop=loop,
            trigger_callback=self._trigger_update,
            ignored_dirs=self._ignored_dirs,
            git_dir=self.git_dir,
        )

        self.observer = Observer()
        # Schedule workspace watcher
        self.observer.schedule(event_handler, path=self.workspace_path, recursive=True)
        # Also watch absolute git dir if it's located outside the workspace (e.g. worktrees)
        if not self.git_dir.startswith(self.workspace_path):
            self.observer.schedule(event_handler, path=self.git_dir, recursive=True)

        self.observer.start()
        logger.info("git_watcher_started", path=self.workspace_path, git_dir=self.git_dir)

    async def stop(self) -> None:
        """Stop the filesystem observer and clean up pending tasks."""
        if self.observer:
            self.observer.stop()
            # Observer join must run in a thread because it's blocking
            await asyncio.to_thread(self.observer.join)
            self.observer = None
            logger.info("git_watcher_stopped")

        if self._debounce_task:
            self._debounce_task.cancel()
            try:
                await self._debounce_task
            except asyncio.CancelledError:
                pass
            self._debounce_task = None

    def _trigger_update(self) -> None:
        """Trigger update_state with 500ms debounce."""
        if self._debounce_task:
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._delayed_update(0.5))

    async def _delayed_update(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self.update_state()
        except asyncio.CancelledError:
            pass

    async def _run_git(self, args: list[str], cwd: str) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.warning(
                    "git_command_failed",
                    args=args,
                    cwd=cwd,
                    stderr=stderr.decode().strip(),
                    code=proc.returncode,
                )
                return ""
            return stdout.decode().rstrip()
        except Exception as e:
            logger.error("git_command_exception", args=args, cwd=cwd, error=str(e))
            return ""

    async def update_state(self) -> None:
        """Query git worktrees and file statuses, then update the SQLite DB."""
        if not self._is_git_repo:
            return

        # Cache the current active branch name
        head_branch = await self._run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"], self.workspace_path
        )
        self.current_branch = head_branch.strip() if head_branch else ""
        if self.current_branch == "HEAD":
            commit = await self._run_git(["rev-parse", "HEAD"], self.workspace_path)
            commit = commit.strip()
            self.current_branch = f"DETACHED_{commit[:8]}" if commit else "DETACHED"

        wt_output = await self._run_git(["worktree", "list", "--porcelain"], self.workspace_path)
        if not wt_output:
            # Fallback to single worktree if git worktree command failed/returned empty
            # which can happen on older git versions, but we should parse HEAD
            head_commit = await self._run_git(["rev-parse", "HEAD"], self.workspace_path)
            head_branch = await self._run_git(
                ["rev-parse", "--abbrev-ref", "HEAD"], self.workspace_path
            )
            worktrees = [
                {
                    "path": self.workspace_path,
                    "commit": head_commit.strip(),
                    "branch": head_branch.strip() if head_branch.strip() != "HEAD" else "",
                }
            ]
        else:
            worktrees = parse_worktrees(wt_output)

        active_branch_names = []
        async with aiosqlite.connect(self.db_path) as db:
            # Set WAL & synchronous mode again for safety on this connection
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA synchronous=NORMAL;")

            for wt in worktrees:
                wt_path = wt["path"]
                commit = wt["commit"]
                branch = wt["branch"]

                if not branch:
                    branch = f"DETACHED_{commit[:8]}" if commit else "DETACHED"

                active_branch_names.append(branch)

                # Update active_branches
                await db.execute(
                    """
                    INSERT OR REPLACE INTO active_branches (branch_name, commit_hash, last_updated)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    """,
                    (branch, commit),
                )

                # Get status of modified files in this worktree
                status_output = await self._run_git(["status", "--porcelain"], wt_path)
                modified_files = parse_git_status(status_output)

                # Filter out ignored directories
                filtered_files = []
                for file_path, status in modified_files:
                    path_parts = Path(file_path).parts
                    if any(ignored in path_parts for ignored in self._ignored_dirs):
                        continue
                    filtered_files.append((file_path, status))

                # Clear previous modified files for this branch
                await db.execute("DELETE FROM modified_files WHERE branch_name = ?", (branch,))

                # Insert new modified files
                if filtered_files:
                    await db.executemany(
                        """
                        INSERT INTO modified_files (branch_name, file_path, status, last_updated)
                        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        [(branch, file_path, status) for file_path, status in filtered_files],
                    )

            # Prune old branches/files that are no longer active
            if active_branch_names:
                placeholders = ",".join("?" for _ in active_branch_names)
                await db.execute(
                    f"DELETE FROM active_branches WHERE branch_name NOT IN ({placeholders})",
                    active_branch_names,
                )
                await db.execute(
                    f"DELETE FROM modified_files WHERE branch_name NOT IN ({placeholders})",
                    active_branch_names,
                )

            await db.commit()
            logger.debug("ledger_updated", active_branches=active_branch_names)
