"""Writes AST nodes for the files a merged pull request changed."""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.code.providers.github.rest_client import GitHubRestClient
from checkcord.ast.parser import TreeSitterParser
from checkcord.bus.interface import EventBus
from checkcord.models.pr_merge import PRMergedPayload
from checkcord.services import code_links
from checkcord.services.ast_writer import is_excluded_path, write_ast_nodes_for_file
from checkcord.services.github_tokens import github_token_for
from checkcord.storage.postgres import PostgresEventStore, tenant_context

logger = structlog.get_logger(__name__).bind(component="ast_writer_worker")


class ASTWriterWorker:
    """Writes AST nodes for every file a merged pull request changed."""

    def __init__(
        self,
        bus: EventBus,
        engine: AsyncEngine,
        group_name: str = "ast_writer_workers",
        consumer_name: str = "ast-writer-worker-1",
        fetch_concurrency: int = 10,
    ) -> None:
        self.bus = bus
        self.engine = engine
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.parser = TreeSitterParser()
        self._fetch_concurrency = fetch_concurrency
        self.stop_event = asyncio.Event()

    async def process_message(self, payload: PRMergedPayload) -> None:
        log = logger.bind(
            tenant_id=str(payload.tenant_id),
            repository=payload.repository_full_name,
            pull_request_number=payload.pull_request_number,
            merge_commit_sha=payload.merge_commit_sha,
        )
        log.info("ast_writer_worker.processing_merge")

        if "/" not in payload.repository_full_name:
            log.error("ast_writer_worker.malformed_repo_identifier")
            raise ValueError(f"Malformed repo_identifier: {payload.repository_full_name!r}")
        owner, repo = payload.repository_full_name.split("/", 1)
        client = GitHubRestClient(
            await github_token_for(self.engine, payload.tenant_id, payload.repository_full_name)
        )

        files_changed, _, _ = await client.get_pull_request_files(
            owner, repo, payload.pull_request_number
        )
        if not files_changed:
            log.info("ast_writer_worker.no_files_changed_or_fetch_failed")
            return

        # Skip vendored/build/generated paths before fetching their content,
        # which saves a GitHub API call per excluded file.
        files_changed = [p for p in files_changed if not is_excluded_path(p)]
        if not files_changed:
            log.info("ast_writer_worker.no_non_excluded_files")
            return

        semaphore = asyncio.Semaphore(self._fetch_concurrency)

        async def fetch_and_write(path: str, store: PostgresEventStore) -> int:
            async with semaphore:
                # Merge commit SHA, not the PR head SHA: squash and rebase
                # merges can differ.
                content = await client.get_file_content(owner, repo, path, payload.merge_commit_sha)
                if content is None:
                    return 0
                result = await write_ast_nodes_for_file(
                    tenant_id=payload.tenant_id,
                    repo_identifier=payload.repository_full_name,
                    file_path=path,
                    content=content,
                    store=store,
                    parser=self.parser,
                )
                return result.nodes_written

        try:
            async with tenant_context(self.engine, payload.tenant_id) as conn:
                store = PostgresEventStore(conn)
                written_counts = await asyncio.gather(
                    *(fetch_and_write(path, store) for path in files_changed)
                )
            await code_links.link_repository(
                self.engine, payload.tenant_id, payload.repository_full_name
            )
        except Exception as e:
            log.exception("ast_writer_worker.write_failed", error=str(e))
            raise

        log.info(
            "ast_writer_worker.completed",
            files_changed=len(files_changed),
            total_nodes_written=sum(written_counts),
        )

    async def _subscribe_to_topic(self, topic: str) -> None:
        try:
            await self.bus.subscribe(
                topic=topic,
                group=self.group_name,
                consumer_name=self.consumer_name,
                handler=self.process_message,
                payload_type=PRMergedPayload,
            )
        except asyncio.CancelledError:
            logger.info("ast_writer_worker.subscription_cancelled", topic=topic)
        except Exception as e:
            logger.exception("ast_writer_worker.subscription_crashed", topic=topic, error=str(e))

    async def start(self, topics: list[str]) -> None:
        logger.info("ast_writer_worker.starting", topics=topics, group=self.group_name)
        tasks = [asyncio.create_task(self._subscribe_to_topic(topic)) for topic in topics]
        await self.stop_event.wait()
        logger.info("ast_writer_worker.shutdown_initiated")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("ast_writer_worker.shutdown_complete")

    def stop(self) -> None:
        self.stop_event.set()
