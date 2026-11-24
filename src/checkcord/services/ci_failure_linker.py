"""File-level CI-failure-to-AST linking.

Resolves which records and AST nodes a CI failure most likely belongs to:
commit SHA -> associated PR -> the PR's changed files (narrowed to the
extractor's reported file path when it is one of them) -> active
AST-node/record edges for those files.

Uses ``PostgresEventStore.find_ast_nodes_by_paths``, not
``ContextMatcherService.match_contexts``: the matcher reports symbols
whose hash differs from the stored one, and for committed code that is
nothing.
"""

from uuid import UUID

import structlog

from checkcord.adapters.code.providers.github.rest_client import GitHubRestClient
from checkcord.core.exceptions import GitHubAPIError
from checkcord.storage.postgres import PostgresEventStore

logger = structlog.get_logger(__name__).bind(component="ci_failure_linker")


async def link_ci_failure_to_contexts(
    tenant_id: UUID,
    repo_identifier: str,
    commit_sha: str,
    ci_event_id: UUID,
    pat: str,
    store: PostgresEventStore,
    extracted_file_path: str | None = None,
) -> int:
    """Best-effort: resolves the PR for ``commit_sha``, its changed files, and
    links ``ci_event_id`` to every canonical context with an active AST edge
    on those files. Returns the number of new links created.

    Logs and returns 0 rather than raising
    whenever no PR resolves for the commit (a direct push to the default
    branch, or a scheduled/manual workflow run, has none) or no AST nodes
    exist yet for the affected files. Never raises for a GitHub API failure
    either; a failed linking attempt must not fail the containing CI-failure
    ingestion event.
    """
    log = logger.bind(
        tenant_id=str(tenant_id),
        repo_identifier=repo_identifier,
        commit_sha=commit_sha,
        ci_event_id=str(ci_event_id),
    )

    if "/" not in repo_identifier:
        log.warning("ci_failure_linker.malformed_repo_identifier")
        return 0

    owner, repo = repo_identifier.split("/", 1)
    client = GitHubRestClient(pat)

    try:
        prs = await client.list_pulls_associated_with_commit(owner, repo, commit_sha)
    except GitHubAPIError as e:
        log.warning("ci_failure_linker.pr_resolution_failed", error=str(e))
        return 0

    if not prs:
        log.info("ci_failure_linker.no_associated_pr")
        return 0

    # A commit can be associated with more than one PR (e.g. re-merged
    # branches, backport PRs); the most recently updated one is the most
    # likely cause of a failure surfacing now.
    pr = max(prs, key=lambda p: p.get("updated_at") or "")
    pr_number = pr.get("number")
    if pr_number is None:
        log.warning("ci_failure_linker.pr_missing_number")
        return 0

    changed_files, _, _ = await client.get_pull_request_files(owner, repo, pr_number)
    if not changed_files:
        log.info("ci_failure_linker.pr_has_no_files", pull_request_number=pr_number)
        return 0

    if extracted_file_path and extracted_file_path in changed_files:
        candidate_files = [extracted_file_path]
    else:
        candidate_files = changed_files

    rows = await store.find_ast_nodes_by_paths(
        tenant_id=tenant_id, repo_identifier=repo_identifier, file_paths=candidate_files
    )

    links_created = 0
    seen: set[tuple[UUID, str]] = set()
    for row in rows:
        canonical_id = row.get("canonical_id")
        if canonical_id is None:
            continue
        key = (canonical_id, row["file_path"])
        if key in seen:
            continue
        seen.add(key)
        inserted = await store.create_ci_failure_context_link(
            tenant_id=tenant_id,
            ci_event_id=ci_event_id,
            canonical_id=canonical_id,
            file_path=row["file_path"],
            ast_node_id=row.get("ast_node_id"),
        )
        if inserted:
            links_created += 1

    log.info(
        "ci_failure_linker.completed",
        links_created=links_created,
        pull_request_number=pr_number,
        candidate_file_count=len(candidate_files),
    )
    return links_created
