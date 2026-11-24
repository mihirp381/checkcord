import asyncio
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.code.providers.github.rest_client import GitHubRestClient
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.models.event import CanonicalEvent
from checkcord.services.context_formatter import ContextFormatterService
from checkcord.services.github_pr_service import GitHubPRService
from checkcord.services.github_tokens import github_token_for
from checkcord.services.repo_audience import RepoAudience, resolve_repo_audience
from checkcord.storage.postgres import PostgresEventStore, tenant_context

logger = structlog.get_logger(__name__).bind(component="context_state_compiler")


class ContextStateCompilerWorker:
    """Compiles a repository's active rules into Redis when records are created."""

    def __init__(
        self,
        redis_client: Redis,
        db_engine: AsyncEngine,
        group_name: str = "context_compiler_workers",
        consumer_name: str = "context-compiler-worker-1",
    ) -> None:
        self.redis = redis_client
        self.bus = RedisStreamsEventBus(redis_client)
        self.engine = db_engine
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.stop_event = asyncio.Event()
        self.formatter = ContextFormatterService()
        self.pr_service = GitHubPRService()
        self.debounce_tasks: dict[tuple[UUID, str], asyncio.Task[None]] = {}

    async def _debounced_compile(self, tenant_id: UUID, repo: str) -> None:
        """Helper to run the compilation after a debounce delay."""
        try:
            # Debounce sleep: handles both event storms and allows AST Linker to complete
            await asyncio.sleep(2.0)

            logger.info("context_compiler.starting_debounced_compilation", repo=repo)
            try:
                pat: str | None = await github_token_for(self.engine, tenant_id, repo)
            except LookupError:
                pat = None
            audience = (
                await resolve_repo_audience(self.engine, tenant_id, repo, GitHubRestClient(pat))
                if pat
                else RepoAudience()
            )

            async with tenant_context(self.engine, tenant_id) as conn:
                store = PostgresEventStore(conn)
                compiled = await self.formatter.compile_for_repo(
                    tenant_id, repo, store, audience, conn=conn
                )

            md_key = f"context:compiled:{tenant_id}:{repo}:architecture_md"
            ledger_key = f"context:compiled:{tenant_id}:{repo}:ledger_json"

            await self.redis.set(md_key, compiled.architecture_md)
            await self.redis.set(ledger_key, compiled.ledger_json)

            logger.info(
                "context_compiler.compiled_and_stored",
                repo=repo,
                md_bytes=len(compiled.architecture_md),
                ledger_bytes=len(compiled.ledger_json),
                contexts=len(compiled.context_ids),
            )

            if not pat:
                logger.warning("context_compiler.no_github_token_skipping_pr", repo=repo)
                return
            pr_success = await self.pr_service.propose_context_update(
                repo_full_name=repo,
                architecture_md=compiled.architecture_md,
                ledger_json=compiled.ledger_json,
                pat=pat,
                create_if_absent=bool(compiled.context_ids),
            )
            if pr_success:
                logger.info("context_compiler.pr_proposal_succeeded", repo=repo)
            else:
                logger.error("context_compiler.pr_proposal_failed", repo=repo)

        except asyncio.CancelledError:
            logger.debug("context_compiler.compilation_cancelled", repo=repo)
        except Exception as e:
            logger.exception("context_compiler.compilation_failed", repo=repo, error=str(e))
        finally:
            self.debounce_tasks.pop((tenant_id, repo), None)

    async def process_message(self, event: CanonicalEvent) -> None:
        log = logger.bind(
            tenant_id=str(event.tenant_id),
            event_id=str(event.event_id),
            context_id=event.source_id,
        )

        log.info("context_compiler.received_event")

        try:
            async with tenant_context(self.engine, event.tenant_id) as conn:
                store = PostgresEventStore(conn)
                # Fetch active repositories for compilation
                repos = await store.get_active_repos_for_compilation(event.tenant_id)

            if not repos:
                log.info("context_compiler.no_active_repos_found")
                return

            log.info("context_compiler.scheduling_compilations", repos=repos)

            for repo in repos:
                key = (event.tenant_id, repo)
                if key in self.debounce_tasks:
                    self.debounce_tasks[key].cancel()

                self.debounce_tasks[key] = asyncio.create_task(
                    self._debounced_compile(event.tenant_id, repo)
                )

        except Exception as e:
            log.exception("context_compiler.processing_failed", error=str(e))
            raise

    async def _subscribe_to_topic(self, topic: str) -> None:
        """Background task running the Redis subscription loop."""
        try:
            await self.bus.subscribe(
                topic=topic,
                group=self.group_name,
                consumer_name=self.consumer_name,
                handler=self.process_message,
            )
        except asyncio.CancelledError:
            logger.info("context_compiler.subscription_cancelled", topic=topic)
        except Exception as e:
            logger.exception("context_compiler.subscription_crashed", topic=topic, error=str(e))

    async def start(self, topics: list[str] | None = None) -> None:
        """Starts the subscription and blocks until stopped."""
        if topics is None:
            topics = ["contexts.created"]
        logger.info("context_compiler.starting", topics=topics, group=self.group_name)

        tasks = [asyncio.create_task(self._subscribe_to_topic(topic)) for topic in topics]

        await self.stop_event.wait()
        logger.info("context_compiler.shutdown_initiated")

        for t in tasks:
            t.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("context_compiler.shutdown_complete")

    def stop(self) -> None:
        """Triggers graceful shutdown."""
        self.stop_event.set()
        for task in list(self.debounce_tasks.values()):
            task.cancel()
