"""Flags record-to-code links unused for 180 days in repositories with recent commits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.code.providers.github.rest_client import GitHubRestClient
from checkcord.services.github_tokens import github_token_for
from checkcord.storage.models import GithubInstallation
from checkcord.storage.postgres import (
    PostgresEventStore,
    app_bypass_rls_context,
    tenant_context,
)

logger = structlog.get_logger(__name__).bind(component="staleness_cron")


async def run_staleness_pruning(
    db_engine: AsyncEngine, cutoff_days: int = 180, active_days: int = 30
) -> int:
    """Run the staleness check for every connected GitHub repository."""
    logger.info("staleness_cron.started", cutoff_days=cutoff_days, active_days=active_days)
    total_pruned = 0

    try:
        async with app_bypass_rls_context(db_engine) as conn:
            # Query all GitHub installations
            stmt = select(
                GithubInstallation.tenant_id,
                GithubInstallation.repo_identifier,
            )
            result = await conn.execute(stmt)
            installations = result.fetchall()

        logger.info("staleness_cron.fetched_installations", count=len(installations))

        for row in installations:
            tenant_id = row.tenant_id
            repo_identifier = row.repo_identifier
            log = logger.bind(tenant_id=str(tenant_id), repo_identifier=repo_identifier)
            try:
                token = await github_token_for(db_engine, tenant_id, repo_identifier)
            except LookupError:
                log.warning("staleness_cron.no_github_token_skipping")
                continue

            # Parse owner and repo name from identifier
            parts = repo_identifier.split("/")
            if len(parts) != 2:
                log.error("staleness_cron.invalid_repo_identifier")
                continue

            owner, repo = parts[0], parts[1]

            # Guardrail Check: Check if repository had active commits in the last 30 days
            client = GitHubRestClient(token)
            has_activity = await client.has_recent_commits(owner, repo, since_days=active_days)

            if not has_activity:
                log.info(
                    "staleness_cron.skipping_pruning_no_recent_activity",
                    msg=f"Repository has no commits in the last {active_days} days.",
                )
                continue

            # Run the pruning heuristic under the tenant's RLS context
            cutoff_date = datetime.now(UTC) - timedelta(days=cutoff_days)
            try:
                async with tenant_context(db_engine, tenant_id) as t_conn:
                    store = PostgresEventStore(t_conn)
                    pruned_count = await store.flag_stale_edges(
                        tenant_id=tenant_id,
                        repo_identifier=repo_identifier,
                        cutoff_date=cutoff_date,
                    )
                    total_pruned += pruned_count
                    if pruned_count > 0:
                        log.info("staleness_cron.edges_flagged_stale", count=pruned_count)
            except Exception as ex:
                log.exception("staleness_cron.failed_for_tenant", error=str(ex))

        # Trigger the deprecation PR generator worker
        try:
            from checkcord.workers.deprecation_worker import run_deprecation_pr_generation

            await run_deprecation_pr_generation(db_engine)
        except Exception as ex:
            logger.exception("staleness_cron.deprecation_worker_failed", error=str(ex))

        logger.info("staleness_cron.completed", total_pruned=total_pruned)
        return total_pruned

    except Exception as e:
        logger.exception("staleness_cron.failed", error=str(e))
        raise
