"""Tests for pipeline/personalization.py: a pure function, hand-computed
fixtures, no I/O.
"""

from checkcord.pipeline.personalization import apply_affinity_boost


def test_cold_start_zero_affinity_returns_results_unchanged() -> None:
    """No prior query for this repo: the order is unchanged."""
    results = [
        {"id": "ctx-1", "similarity": 0.9},
        {"id": "ctx-2", "similarity": 0.8},
        {"id": "ctx-3", "similarity": 0.7},
    ]
    boosted = apply_affinity_boost(results, org_global_ids=set(), repo_affinity_query_count=0)
    assert boosted == results


def test_positive_affinity_promotes_repo_scoped_above_org_global() -> None:
    """When the queried repo has affinity, repo-scoped results move above
    org-global ones, without dropping or duplicating anything.
    """
    results = [
        {"id": "org-1", "similarity": 0.95},
        {"id": "repo-1", "similarity": 0.9},
        {"id": "org-2", "similarity": 0.85},
        {"id": "repo-2", "similarity": 0.8},
    ]
    org_global_ids = {"org-1", "org-2"}

    boosted = apply_affinity_boost(results, org_global_ids, repo_affinity_query_count=3)

    assert [r["id"] for r in boosted] == ["repo-1", "repo-2", "org-1", "org-2"]
    # Same 4 ids, none dropped, none duplicated.
    assert {r["id"] for r in boosted} == {r["id"] for r in results}
    assert len(boosted) == len(results)


def test_positive_affinity_preserves_relative_order_within_each_group() -> None:
    """Stable partition, not a re-sort: existing similarity/RRF/rerank order
    inside each group (org-global vs. repo-scoped) is untouched."""
    results = [
        {"id": "repo-3", "similarity": 0.5},
        {"id": "org-3", "similarity": 0.95},
        {"id": "repo-1", "similarity": 0.9},
        {"id": "org-1", "similarity": 0.6},
        {"id": "repo-2", "similarity": 0.7},
    ]
    org_global_ids = {"org-1", "org-3"}

    boosted = apply_affinity_boost(results, org_global_ids, repo_affinity_query_count=1)

    # repo-scoped group keeps its original relative order: repo-3, repo-1, repo-2
    # org-global group keeps its original relative order: org-3, org-1
    assert [r["id"] for r in boosted] == ["repo-3", "repo-1", "repo-2", "org-3", "org-1"]


def test_all_org_global_results_unaffected_by_boost() -> None:
    """Nothing to promote when every result is org-global -- order unchanged."""
    results = [{"id": "org-1"}, {"id": "org-2"}]
    org_global_ids = {"org-1", "org-2"}

    boosted = apply_affinity_boost(results, org_global_ids, repo_affinity_query_count=5)

    assert boosted == results


def test_all_repo_scoped_results_unaffected_by_boost() -> None:
    """Nothing to demote when every result is repo-scoped -- order unchanged."""
    results = [{"id": "repo-1"}, {"id": "repo-2"}]

    boosted = apply_affinity_boost(results, org_global_ids=set(), repo_affinity_query_count=5)

    assert boosted == results


def test_empty_results_returns_empty() -> None:
    assert apply_affinity_boost([], org_global_ids=set(), repo_affinity_query_count=5) == []


def test_negative_affinity_query_count_treated_as_cold_start() -> None:
    """Defensive: a query_count of 0 or less is never a valid affinity
    signal (get_user_repo_affinity never returns negative in practice, but
    the boundary condition is <= 0, not just == 0)."""
    results = [{"id": "org-1"}, {"id": "repo-1"}]
    org_global_ids = {"org-1"}

    boosted = apply_affinity_boost(results, org_global_ids, repo_affinity_query_count=-1)

    assert boosted == results
