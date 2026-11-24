"""Pure re-rank function for personalised retrieval.

A search covers at most one repo, so results are either org-global or
scoped to that repo. When the caller works in the queried repo often,
repo-scoped results are moved ahead of org-global ones. Nothing is
dropped and order within each group is kept.
"""

from __future__ import annotations

from typing import Any


def apply_affinity_boost(
    results: list[dict[str, Any]],
    org_global_ids: set[str],
    repo_affinity_query_count: int,
) -> list[dict[str, Any]]:
    """Put repo-scoped results before org-global ones when the user has queried this repo.

    With ``repo_affinity_query_count == 0`` the results come back unchanged.
    Order within each group is preserved and nothing is added or dropped.
    """
    if repo_affinity_query_count <= 0:
        return results

    repo_scoped = [r for r in results if str(r.get("id")) not in org_global_ids]
    org_global = [r for r in results if str(r.get("id")) in org_global_ids]
    return repo_scoped + org_global
