"""Retires links between records and code that went stale; the records stay active."""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.code.providers.github.rest_client import GitHubRestClient
from checkcord.core import redaction
from checkcord.services.context_formatter import ContextFormatterService
from checkcord.services.github_pr_service import GitHubPRService
from checkcord.services.github_tokens import github_token_for
from checkcord.services.repo_audience import resolve_repo_audience
from checkcord.storage.models import GithubInstallation
from checkcord.storage.postgres import (
    PostgresEventStore,
    app_bypass_rls_context,
    tenant_context,
)

logger = structlog.get_logger(__name__).bind(component="deprecation_worker")


async def run_deprecation_pr_generation(db_engine: AsyncEngine) -> int:
    """Scan all installations for stale edges, generate deprecation PRs,
    and mark edges deprecated.
    """
    logger.info("deprecation_worker.started")
    total_prs_proposed = 0

    try:
        async with app_bypass_rls_context(db_engine) as conn:
            # Query all GitHub installations
            stmt = select(
                GithubInstallation.tenant_id,
                GithubInstallation.repo_identifier,
            )
            result = await conn.execute(stmt)
            installations = result.fetchall()

        logger.info("deprecation_worker.fetched_installations", count=len(installations))

        formatter = ContextFormatterService()
        pr_service = GitHubPRService()

        for row in installations:
            tenant_id = row.tenant_id
            repo_identifier = row.repo_identifier
            log = logger.bind(tenant_id=str(tenant_id), repo_identifier=repo_identifier)
            try:
                token = await github_token_for(db_engine, tenant_id, repo_identifier)
            except LookupError:
                log.warning("deprecation_worker.no_github_token_skipping")
                continue

            try:
                # Fetch stale context edges under tenant context
                async with tenant_context(db_engine, tenant_id) as t_conn:
                    store = PostgresEventStore(t_conn)
                    stale_edges = await store.get_stale_edges_for_repo(
                        tenant_id=tenant_id,
                        repo_identifier=repo_identifier,
                    )

                if not stale_edges:
                    log.info("deprecation_worker.no_stale_edges_skipping")
                    continue

                log.info("deprecation_worker.stale_edges_found", count=len(stale_edges))

                audience = await resolve_repo_audience(
                    db_engine, tenant_id, repo_identifier, GitHubRestClient(token)
                )

                # Compile updated .context files (which will omit the stale edges)
                async with tenant_context(db_engine, tenant_id) as t_conn:
                    store = PostgresEventStore(t_conn)
                    compiled = await formatter.compile_for_repo(
                        tenant_id=tenant_id,
                        repo_identifier=repo_identifier,
                        store=store,
                        audience=audience,
                        conn=t_conn,
                    )

                # Build a detailed deprecation PR description
                pr_body_lines = [
                    "This automated pull request removes these links between a record and "
                    "code from `.context/ARCHITECTURE.md` and `.context/ledger.json`, as none "
                    "has been read or verified in over 180 days. The records stay active; an "
                    "admin retires a record that no longer holds with `checkcord records retire`:",
                    "",
                ]
                for edge in stale_edges:
                    if not audience.allows(edge.get("access_control")):
                        continue
                    summary = redaction.serve(edge["summary"], edge.get("summary_redacted"))
                    pr_body_lines.append(
                        f"- **Symbol**: `{edge['symbol_name']}` in `{edge['file_path']}`  "
                    )
                    pr_body_lines.append(
                        f"  **Context Rule**: {summary} (ID: `{edge['canonical_id']}`)"
                    )
                    pr_body_lines.append("")

                pr_body_lines.append("Please review and merge these changes.")
                pr_body = "\n".join(pr_body_lines)

                pr_success = await pr_service.propose_context_update(
                    repo_full_name=repo_identifier,
                    architecture_md=compiled.architecture_md,
                    ledger_json=compiled.ledger_json,
                    pat=token,
                    branch_name="checkcord/deprecate-stale-context",
                    pr_title="[chore] Deprecate stale architectural context",
                    pr_body=pr_body,
                    create_if_absent=bool(compiled.context_ids),
                )

                if pr_success:
                    # Transition edges to 'deprecated' status in Postgres
                    edge_keys = [(e["ast_node_id"], e["canonical_id"]) for e in stale_edges]
                    async with tenant_context(db_engine, tenant_id) as t_conn:
                        store = PostgresEventStore(t_conn)
                        marked_count = await store.mark_edges_deprecated(
                            tenant_id=tenant_id,
                            edge_keys=edge_keys,
                        )
                    log.info("deprecation_worker.edges_marked_deprecated", count=marked_count)
                    total_prs_proposed += 1
                else:
                    log.error("deprecation_worker.failed_to_propose_pr")

            except Exception as ex:
                log.exception("deprecation_worker.failed_for_installation", error=str(ex))

        logger.info("deprecation_worker.completed", total_prs_proposed=total_prs_proposed)
        return total_prs_proposed

    except Exception as e:
        logger.exception("deprecation_worker.failed", error=str(e))
        raise
