"""Writes AST nodes for every supported file of a newly connected repository."""

from __future__ import annotations

import asyncio
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.code.providers.github.rest_client import GitHubRestClient
from checkcord.ast.parser import TreeSitterParser, get_language_from_filename
from checkcord.bus.interface import EventBus
from checkcord.core.config import get_settings
from checkcord.core.exceptions import IngestionError
from checkcord.models.repo_backfill import RepoBackfillRequestPayload
from checkcord.services import code_links
from checkcord.services.ast_writer import (
    WriteResult,
    is_excluded_path,
    write_ast_nodes_for_file,
)
from checkcord.services.github_tokens import github_token_for
from checkcord.storage.postgres import PostgresEventStore, tenant_context

logger = structlog.get_logger(__name__).bind(component="repo_backfill_worker")

_CURSOR_TTL_SECONDS = 86_400  # 24h safety net so an abandoned cursor doesn't linger forever


def _cursor_key(tenant_id: UUID, repo_identifier: str) -> str:
    return f"backfill:cursor:{tenant_id}:{repo_identifier}"


class RepoBackfillWorker:
    """Walks the full tree of a newly connected repository once, resumably."""

    def __init__(
        self,
        bus: EventBus,
        engine: AsyncEngine,
        redis_client: Redis,
        group_name: str = "repo_backfill_workers",
        consumer_name: str = "repo-backfill-worker-1",
        fetch_concurrency: int = 10,
    ) -> None:
        self.bus = bus
        self.engine = engine
        self.redis = redis_client
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.settings = get_settings()
        self.parser = TreeSitterParser()
        self._fetch_concurrency = fetch_concurrency
        self.stop_event = asyncio.Event()

    async def process_message(self, payload: RepoBackfillRequestPayload) -> None:
        log = logger.bind(
            tenant_id=str(payload.tenant_id),
            repo_identifier=payload.repo_identifier,
        )
        log.info("repo_backfill.processing")

        try:
            pat = await github_token_for(self.engine, payload.tenant_id, payload.repo_identifier)
        except LookupError as err:
            raise IngestionError(
                f"No GithubInstallation found for {payload.repo_identifier} "
                f"under tenant {payload.tenant_id}"
            ) from err

        if "/" not in payload.repo_identifier:
            raise IngestionError(f"Malformed repo_identifier: {payload.repo_identifier!r}")
        owner, repo = payload.repo_identifier.split("/", 1)
        client = GitHubRestClient(pat)

        repo_info = await client.get_repository(owner, repo)
        default_branch = (repo_info or {}).get("default_branch")
        if not default_branch:
            raise IngestionError(f"Could not resolve default branch for {payload.repo_identifier}")

        # Raises GitHubAPIError on failure -- deliberately not caught here, so
        # a transient tree-listing failure retries the whole message via the
        # bus's normal PEL/DLQ machinery rather than silently treating a
        # failed listing as "empty repo" (M15's whole point).
        tree = await client.list_repository_tree(owner, repo, default_branch, recursive=True)

        candidate_paths = sorted(
            {
                entry["path"]
                for entry in tree
                if entry.get("type") == "blob"
                and entry.get("path")
                and not is_excluded_path(entry["path"])
                and get_language_from_filename(entry["path"]) is not None
            }
        )

        cursor_key = _cursor_key(payload.tenant_id, payload.repo_identifier)
        if not candidate_paths:
            log.info("repo_backfill.no_supported_files")
            await self.redis.delete(cursor_key)
            return

        raw_cursor = await self.redis.get(cursor_key)
        start_index = 0
        if raw_cursor is not None:
            try:
                start_index = int(raw_cursor)
            except (TypeError, ValueError):
                start_index = 0

        if start_index >= len(candidate_paths):
            log.info("repo_backfill.already_complete", total_files=len(candidate_paths))
            await code_links.link_repository(
                self.engine, payload.tenant_id, payload.repo_identifier
            )
            await self.redis.delete(cursor_key)
            return

        batch_size = max(1, self.settings.repo_backfill_batch_size)
        semaphore = asyncio.Semaphore(self._fetch_concurrency)

        async def fetch_and_write(path: str, store: PostgresEventStore) -> WriteResult:
            async with semaphore:
                content = await client.get_file_content(owner, repo, path, default_branch)
                if content is None:
                    return WriteResult(
                        file_path=path, skipped=True, skip_reason="fetch_failed_or_missing"
                    )
                return await write_ast_nodes_for_file(
                    tenant_id=payload.tenant_id,
                    repo_identifier=payload.repo_identifier,
                    file_path=path,
                    content=content,
                    store=store,
                    parser=self.parser,
                )

        total_written = 0
        total_skipped = 0
        index = start_index
        while index < len(candidate_paths):
            batch = candidate_paths[index : index + batch_size]
            # One transaction per batch: a batch must commit before the cursor
            # advances, or a crash would leave the cursor ahead of what was
            # written.
            async with tenant_context(self.engine, payload.tenant_id) as conn:
                store = PostgresEventStore(conn)
                results = await asyncio.gather(*(fetch_and_write(p, store) for p in batch))

            total_written += sum(r.nodes_written for r in results)
            total_skipped += sum(1 for r in results if r.skipped)
            index += len(batch)
            await self.redis.set(cursor_key, str(index), ex=_CURSOR_TTL_SECONDS)

            log.info(
                "repo_backfill.batch_complete",
                processed=index,
                total=len(candidate_paths),
            )

        await code_links.link_repository(self.engine, payload.tenant_id, payload.repo_identifier)
        await self.redis.delete(cursor_key)
        log.info(
            "repo_backfill.completed",
            total_files=len(candidate_paths),
            total_nodes_written=total_written,
            total_skipped=total_skipped,
        )

    async def _ensure_group(self, topic: str, group: str) -> None:
        try:
            await self.redis.xgroup_create(name=topic, groupname=group, id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" in str(e):
                pass
            else:
                raise

    async def _subscribe_to_topic(self, topic: str) -> None:
        try:
            await self.bus.subscribe(
                topic=topic,
                group=self.group_name,
                consumer_name=self.consumer_name,
                handler=self.process_message,
                payload_type=RepoBackfillRequestPayload,
            )
        except asyncio.CancelledError:
            logger.info("repo_backfill.subscription_cancelled", topic=topic)
        except Exception as e:
            logger.exception("repo_backfill.subscription_crashed", topic=topic, error=str(e))

    async def start(self, topics: list[str]) -> None:
        logger.info("repo_backfill.starting", topics=topics, group=self.group_name)
        tasks = [asyncio.create_task(self._subscribe_to_topic(topic)) for topic in topics]
        await self.stop_event.wait()
        logger.info("repo_backfill.shutdown_initiated")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("repo_backfill.shutdown_complete")

    def stop(self) -> None:
        self.stop_event.set()
