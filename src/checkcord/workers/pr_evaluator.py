import asyncio
import time
import traceback
from typing import Any
from uuid import UUID

import structlog
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.code.providers.github.parser import OWN_BRANCH_PREFIX
from checkcord.adapters.code.providers.github.rest_client import GitHubRestClient
from checkcord.ast.hasher import SemanticHasher
from checkcord.ast.parser import TreeSitterParser, get_language_from_filename
from checkcord.bus import pending
from checkcord.bus.dead_letter import write_dead_letter
from checkcord.core.config import get_settings
from checkcord.models.pr_checks import AffectedContext, PRContextCheckedPayload
from checkcord.services.context_matcher import ContextMatcherService
from checkcord.services.github_tokens import github_token_for

logger = structlog.get_logger(__name__).bind(component="pr_evaluator_worker")

__all__ = ["PREvaluatorService", "PRWorker", "build_diff", "get_language_from_filename"]

DIFF_FILES = 30
FILE_DIFF_CHARS = 6000
DIFF_CHARS = 24000


def _cut(text: str, limit: int, what: str) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[cut: {len(text) - limit} more characters of {what}]"


def build_diff(files: list[dict[str, Any]]) -> str:
    """The pull request's patches as one diff the model can read, within fixed limits."""
    parts: list[str] = []
    used = 0
    shown = files[:DIFF_FILES]
    nameless = [f["filename"] for f in shown if not f.get("patch")]
    for n, f in enumerate(f for f in shown if f.get("patch")):
        name = f["filename"]
        block = f"--- a/{name}\n+++ b/{name}\n" + _cut(f["patch"], FILE_DIFF_CHARS, name)
        if used + len(block) > DIFF_CHARS:
            left = len([g for g in shown if g.get("patch")]) - n
            parts.append(_cut(block, max(DIFF_CHARS - used, 0), name))
            parts.append(f"[cut: diff limit reached; {left - 1} more files not shown]")
            break
        parts.append(block)
        used += len(block)
    if nameless:
        parts.append("Changed without a diff: " + ", ".join(nameless))
    if len(files) > DIFF_FILES:
        parts.append(f"[cut: {len(files) - DIFF_FILES} more files not shown]")
    return "\n".join(parts)


class PREvaluatorService:
    def __init__(self) -> None:
        self.parser = TreeSitterParser()

    async def evaluate_pr(
        self,
        repo_name: str,
        pull_number: int,
        commit_sha: str,
        pat: str,
    ) -> dict[str, Any]:
        """Fetch changed files, pull contents, and parse AST to extract symbols."""
        owner, repo = repo_name.split("/")
        client = GitHubRestClient(pat)

        files = await client.list_pull_request_files(owner, repo, pull_number)
        files_changed = [f["filename"] for f in files]
        diff = build_diff(files)
        if not files_changed:
            logger.info("no_files_changed", repo=repo_name, pr=pull_number)
            return {"results": [], "analyzed_files": [], "diff": diff}

        # Filter files by extension
        supported_files = []
        for f in files_changed:
            lang = get_language_from_filename(f)
            if lang:
                supported_files.append((f, lang))

        if not supported_files:
            logger.info("no_supported_files_in_pr", repo=repo_name, pr=pull_number)
            return {"results": [], "analyzed_files": [], "diff": diff}

        # Defensive cap: Process at most 50 files to prevent rate-limit blocks and memory exhaustion
        if len(supported_files) > 50:
            logger.warning(
                "pr_files_exceed_cap",
                repo=repo_name,
                pr=pull_number,
                count=len(supported_files),
                cap=50,
            )
            supported_files = supported_files[:50]

        # Limit concurrency to avoid secondary rate limits
        semaphore = asyncio.Semaphore(10)
        results = []

        async def fetch_and_parse(path: str, lang: str) -> None:
            async with semaphore:
                content = await client.get_file_content(owner, repo, path, commit_sha)
                if content is None:
                    return
                try:
                    nodes = self.parser.extract_nodes(content, lang)
                    hashed_nodes = SemanticHasher.generate_hashes(nodes, lang)
                    for node in hashed_nodes:
                        results.append((path, node))
                except Exception as e:
                    logger.warning("ast_parsing_failed", path=path, error=str(e))

        # Fetch and parse concurrently
        tasks = [fetch_and_parse(path, lang) for path, lang in supported_files]
        await asyncio.gather(*tasks)

        return {
            "results": results,
            "analyzed_files": [path for path, _ in supported_files],
            "diff": diff,
        }


class PRWorker:
    def __init__(self, redis_client: Redis, db_engine: AsyncEngine | None = None) -> None:
        self.redis = redis_client
        self.db_engine = db_engine
        self.settings = get_settings()
        self.batch_size = self.settings.redis_stream_batch_size
        self.poll_timeout = self.settings.redis_stream_poll_timeout_ms
        self.service = PREvaluatorService()
        self.matcher = ContextMatcherService()

    async def _ensure_group(self, topic: str, group: str) -> None:
        try:
            await self.redis.xgroup_create(name=topic, groupname=group, id="0", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" in str(e):
                pass
            else:
                raise

    async def start(self) -> None:
        topic = "ci.pr.evaluate"
        group = "pr_evaluation_group"
        consumer_name = "pr_evaluator_consumer"
        await self._ensure_group(topic, group)
        logger.info("pr_evaluator.started", topic=topic, group=group)

        next_reclaim = 0.0
        while True:
            try:
                now = time.monotonic()
                if now >= next_reclaim:
                    next_reclaim = now + self.settings.redis_stream_reclaim_interval_ms / 1000
                    reclaimed = await pending.reclaim(
                        self.redis,
                        topic,
                        group,
                        consumer_name,
                        min_idle_ms=self.settings.redis_stream_reclaim_min_idle_ms,
                        count=self.batch_size,
                    )
                    for msg_id, raw_payload in reclaimed:
                        await self._process_message(topic, group, msg_id, raw_payload)
                batch = pending.entries(
                    await self.redis.xreadgroup(
                        groupname=group,
                        consumername=consumer_name,
                        streams={topic: ">"},
                        count=self.batch_size,
                        block=self.poll_timeout,
                    )
                )
                for msg_id, raw_payload in batch:
                    await self._process_message(topic, group, msg_id, raw_payload)

            except asyncio.CancelledError:
                logger.info("pr_evaluator.cancelled")
                break
            except Exception as e:
                logger.exception("pr_evaluator.loop_error", error=str(e))
                await asyncio.sleep(2)

    async def _process_message(
        self, topic: str, group: str, msg_id: str, raw_payload: dict[str, str]
    ) -> None:
        try:
            repo_name = raw_payload["repository_full_name"]
            pull_number = int(raw_payload["pull_request_number"])
            commit_sha = raw_payload["head_sha"]
            tenant_id_str = raw_payload.get("tenant_id", "00000000-0000-0000-0000-000000000000")
            tenant_id = UUID(tenant_id_str)
            head_ref = raw_payload.get("head_ref", "")
            if head_ref.startswith(OWN_BRANCH_PREFIX):
                eval_res: dict[str, Any] = {"results": [], "analyzed_files": [], "diff": ""}
            else:
                pat = await github_token_for(self.db_engine, tenant_id, repo_name)
                logger.info(
                    "processing_pr_evaluation",
                    repo=repo_name,
                    pr=pull_number,
                    sha=commit_sha,
                    tenant_id=tenant_id_str,
                )
                eval_res = await self.service.evaluate_pr(repo_name, pull_number, commit_sha, pat)
            results = eval_res["results"]
            analyzed_files = eval_res["analyzed_files"]
            logger.info(
                "pr_evaluation_complete",
                repo=repo_name,
                pr=pull_number,
                sha=commit_sha,
                symbols_count=len(results),
            )

            affected_contexts = []
            if self.db_engine and analyzed_files:
                from checkcord.storage.postgres import PostgresEventStore, tenant_context

                async with tenant_context(self.db_engine, tenant_id) as conn:
                    store = PostgresEventStore(conn)
                    affected_contexts = await self.matcher.match_contexts(
                        tenant_id=tenant_id,
                        repo_identifier=repo_name,
                        file_paths=analyzed_files,
                        pr_nodes=results,
                        store=store,
                    )

            # Publish affected contexts to ci.pr.context_checked as one typed payload.
            checked_payload = PRContextCheckedPayload(
                repository_full_name=repo_name,
                pull_request_number=pull_number,
                head_sha=commit_sha,
                tenant_id=tenant_id,
                affected_contexts=[AffectedContext(**d) for d in affected_contexts],
                head_ref=head_ref,
                diff=eval_res.get("diff", ""),
            )
            await self.redis.xadd(
                "ci.pr.context_checked",
                fields={"payload": checked_payload.model_dump_json()},
            )
            logger.info(
                "pr_context_checked_published",
                repo=repo_name,
                pr=pull_number,
                sha=commit_sha,
                affected_contexts_count=len(affected_contexts),
            )

            # ACK message
            await self.redis.xack(topic, group, msg_id)

        except Exception as e:
            logger.exception("failed_to_process_pr_evaluation_message", msg_id=msg_id, error=str(e))
            await self._handle_dlq(topic, group, msg_id, raw_payload, traceback.format_exc())
            await self._not_evaluated(raw_payload)

    async def _not_evaluated(self, raw_payload: dict[str, str]) -> None:
        from checkcord.workers.pr_status_reporter import post_not_evaluated

        try:
            await post_not_evaluated(
                self.redis,
                self.db_engine,
                UUID(raw_payload["tenant_id"]),
                raw_payload["repository_full_name"],
                raw_payload["head_sha"],
            )
        except Exception as e:
            logger.error("pr_check.error_post_failed", error=str(e)[:300])

    async def _handle_dlq(
        self, topic: str, group: str, msg_id: str, raw_payload: dict[str, str], error_msg: str
    ) -> None:
        await write_dead_letter(self.redis, topic, raw_payload, error_msg)
        await self.redis.xack(topic, group, msg_id)
