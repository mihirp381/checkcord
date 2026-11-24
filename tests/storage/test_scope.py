"""Tests for the context scoping model.

A context with no repo link is org-global; a context bootstrapped from a
code-linked event is scoped to that repo. At retrieval, a request scoped to
repo X sees org-global contexts plus repo-X ones, never another repo's, and
"no repo specified" or an unknown repo never means "everything."
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text

from checkcord.core.config import get_settings
from checkcord.storage.postgres import (
    PostgresEventStore,
    create_checkcord_engine,
    tenant_context,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _repo_scopes(rows: list[dict[str, str]]) -> list[str]:
    return [r["scope_value"] for r in rows if r["scope_type"] == "repo"]


async def test_save_canonical_context_no_repo_defaults_to_org_global() -> None:
    """A context with no originating repo (e.g. Slack/Jira-only) is org-global."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Chat-only decision, no code link",
                confidence_score=0.0,
            )
            ctx = await store.get_canonical_context(tenant_id, ctx_id)
            assert ctx is not None
            # Org-global means zero context_repo_scopes rows.
            assert _repo_scopes(await store.get_context_scope_rows(tenant_id, ctx_id)) == []
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_save_canonical_context_with_repo_sets_scope() -> None:
    """A context bootstrapped from a code-linked event is scoped to that repo."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="A PR discussion about acme/service-a",
                confidence_score=0.0,
                scope_repo_identifier="acme/service-a",
            )
            ctx = await store.get_canonical_context(tenant_id, ctx_id)
            assert ctx is not None
            # Exactly one context_repo_scopes row for a single-repo context.
            assert _repo_scopes(await store.get_context_scope_rows(tenant_id, ctx_id)) == [
                "acme/service-a"
            ]
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_link_context_to_event_does_not_change_scope() -> None:
    """Scope is sticky: linking additional events never narrows or widens it."""
    from checkcord.models.enums import SourcePlatform, SourceType
    from checkcord.models.event import CanonicalEvent

    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Repo-scoped decision",
                confidence_score=0.0,
                scope_repo_identifier="acme/service-a",
            )

            other_event = CanonicalEvent(
                tenant_id=tenant_id,
                source_platform=SourcePlatform.SLACK,
                source_type=SourceType.MESSAGE,
                source_id=f"msg_{uuid4().hex[:8]}",
                happened_at=datetime.now(UTC),
                author={"source_id": "user_test"},
                content_raw="Corroborating chat message",
                content_clean="Corroborating chat message",
                access_control={"is_public": True},
            )
            await store.save_event(other_event)
            await store.link_context_to_event(tenant_id, ctx_id, other_event.event_id)

            ctx = await store.get_canonical_context(tenant_id, ctx_id)
            assert ctx is not None
            assert _repo_scopes(await store.get_context_scope_rows(tenant_id, ctx_id)) == [
                "acme/service-a"
            ]
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_event_links"))
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM events WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_get_top_contexts_for_repo_returns_org_global_plus_repo_local() -> None:
    """Repo X sees org-global + X-local, never Y-local."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Org-wide rule",
                confidence_score=5.0,
                scope_repo_identifier=None,
            )
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Service-a-local rule",
                confidence_score=4.0,
                scope_repo_identifier="acme/service-a",
            )
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Service-b-local rule",
                confidence_score=3.0,
                scope_repo_identifier="acme/service-b",
            )

            results = await store.get_top_contexts_for_repo(tenant_id, "acme/service-a")
            summaries = {r["summary"] for r in results}

            assert summaries == {"Org-wide rule", "Service-a-local rule"}
            assert "Service-b-local rule" not in summaries
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_get_top_contexts_for_repo_respects_ast_drift_independently_of_scope() -> None:
    """Scope filtering must not swallow the drift filter.

    A context whose only AST link into the queried repo has gone STALE is
    excluded for that repo whatever its scope. A context scoped to the repo
    with no AST link at all is unaffected.
    """
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    repo_id = "acme/service-a"

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)

            # Org-global context, AST-linked to repo_id (edge starts ACTIVE).
            drifting_ctx_id = uuid4()
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Code-linked rule that will drift",
                confidence_score=9.0,
                id=drifting_ctx_id,
            )
            await store.upsert_ast_node_and_check_drift(
                tenant_id=tenant_id,
                repo_identifier=repo_id,
                file_path="src/a.py",
                symbol_name="do_thing",
                node_type="function",
                merkle_hash="hash_v1",
            )
            nodes = await store.find_ast_nodes_by_symbols(tenant_id, ["do_thing"])
            node_id = nodes[0]["id"]
            await store.link_ast_to_context(tenant_id, node_id, drifting_ctx_id)

            # Scoped context with no AST link at all -- drift is not applicable.
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Scoped rule with no code link",
                confidence_score=8.0,
                scope_repo_identifier=repo_id,
            )

            results_before = await store.get_top_contexts_for_repo(tenant_id, repo_id)
            assert {r["summary"] for r in results_before} == {
                "Code-linked rule that will drift",
                "Scoped rule with no code link",
            }

            # Trigger drift: re-sync the same node with a different hash.
            await store.upsert_ast_node_and_check_drift(
                tenant_id=tenant_id,
                repo_identifier=repo_id,
                file_path="src/a.py",
                symbol_name="do_thing",
                node_type="function",
                merkle_hash="hash_v2",
            )

            results_after = await store.get_top_contexts_for_repo(tenant_id, repo_id)
            summaries_after = {r["summary"] for r in results_after}
            assert summaries_after == {"Scoped rule with no code link"}
            assert "Code-linked rule that will drift" not in summaries_after
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM ast_context_edges"))
            await conn.execute(text("DELETE FROM ast_nodes"))
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_get_top_contexts_for_repo_unknown_repo_returns_org_global_only() -> None:
    """A malformed/unknown repo_identifier returns org-global-only, not empty, not everything."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Org-wide rule",
                confidence_score=5.0,
                scope_repo_identifier=None,
            )
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Service-a-local rule",
                confidence_score=4.0,
                scope_repo_identifier="acme/service-a",
            )

            results = await store.get_top_contexts_for_repo(tenant_id, "totally/unknown-repo")
            summaries = {r["summary"] for r in results}

            assert summaries == {"Org-wide rule"}
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_get_top_contexts_for_repo_zero_locally_scoped_still_returns_org_global() -> None:
    """A repo with zero locally-scoped contexts must still return the org-global set."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Org-wide rule A",
                confidence_score=5.0,
            )
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Org-wide rule B",
                confidence_score=4.0,
            )

            results = await store.get_top_contexts_for_repo(tenant_id, "acme/never-scoped-repo")
            summaries = {r["summary"] for r in results}

            assert summaries == {"Org-wide rule A", "Org-wide rule B"}
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_find_similar_contexts_omitting_repo_returns_org_global_only() -> None:
    """Vector search: omitting repo_identifier is org-global-only, never every repo."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    embedding = [0.1] * 1024

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Org-wide insight",
                confidence_score=5.0,
                embedding=embedding,
            )
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Repo-local insight",
                confidence_score=4.0,
                embedding=embedding,
                scope_repo_identifier="acme/service-a",
            )

            results_no_repo = await store.find_similar_contexts(tenant_id, embedding, threshold=0.5)
            summaries_no_repo = {r["summary"] for r in results_no_repo}
            assert summaries_no_repo == {"Org-wide insight"}

            results_with_repo = await store.find_similar_contexts(
                tenant_id, embedding, threshold=0.5, repo_identifier="acme/service-a"
            )
            summaries_with_repo = {r["summary"] for r in results_with_repo}
            assert summaries_with_repo == {"Org-wide insight", "Repo-local insight"}

            results_other_repo = await store.find_similar_contexts(
                tenant_id, embedding, threshold=0.5, repo_identifier="acme/service-b"
            )
            summaries_other_repo = {r["summary"] for r in results_other_repo}
            assert summaries_other_repo == {"Org-wide insight"}
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


# ---------------------------------------------------------------------------
# context_repo_scopes junction table: a context can be scoped to more than
# one repo. Drift is checked per repo in the set, not once per context.
# ---------------------------------------------------------------------------


async def test_context_scoped_to_multiple_repos_appears_in_both_never_a_third() -> None:
    """A context scoped to [service-a, service-b] appears in queries for
    either repo and is absent from a query for service-c.
    """
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Shared-library ADR referenced from two services",
                confidence_score=6.0,
            )
            updated = await store.update_context_scope(
                tenant_id=tenant_id,
                canonical_id=ctx_id,
                repo_identifiers=["acme/service-a", "acme/service-b"],
                is_locked=False,
                changed_by="alice@acme.example",
            )
            assert updated is not None
            assert sorted(updated["scope_repo_identifiers"]) == [
                "acme/service-a",
                "acme/service-b",
            ]

            results_a = await store.get_top_contexts_for_repo(tenant_id, "acme/service-a")
            results_b = await store.get_top_contexts_for_repo(tenant_id, "acme/service-b")
            results_c = await store.get_top_contexts_for_repo(tenant_id, "acme/service-c")

            assert {r["id"] for r in results_a} == {str(ctx_id)}
            assert {r["id"] for r in results_b} == {str(ctx_id)}
            assert {r["id"] for r in results_c} == set()
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_repo_scopes"))
            await conn.execute(text("DELETE FROM context_scope_audit_log"))
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_save_canonical_context_retry_replaces_scope_not_accumulates() -> None:
    """save_canonical_context writes a single repo per call and replaces the
    junction row set on a retry for the same id: the latest write wins.
    """
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    ctx_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="First write",
                confidence_score=1.0,
                id=ctx_id,
                scope_repo_identifier="acme/service-a",
            )
            assert _repo_scopes(await store.get_context_scope_rows(tenant_id, ctx_id)) == [
                "acme/service-a"
            ]

            # Retry with the same id but a different repo -- must replace,
            # not accumulate to two rows.
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Retried write",
                confidence_score=1.0,
                id=ctx_id,
                scope_repo_identifier="acme/service-b",
            )
            assert _repo_scopes(await store.get_context_scope_rows(tenant_id, ctx_id)) == [
                "acme/service-b"
            ]

            # Retry with no repo at all -- must clear back to org-global.
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Retried write, now org-global",
                confidence_score=1.0,
                id=ctx_id,
            )
            assert _repo_scopes(await store.get_context_scope_rows(tenant_id, ctx_id)) == []
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_repo_scopes"))
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


# ---------------------------------------------------------------------------
# Four scope tiers: org/team/repo/path.
# ---------------------------------------------------------------------------


async def test_scope_row_priority_is_server_computed_per_tier() -> None:
    """4a: priority is derived from scope_type (path=3 > repo=2 > team=1),
    never caller-set -- verified directly against the stored column, since
    no caller reads it back through a Python-level accessor."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id, summary="Priority check", confidence_score=1.0
            )
            await store.update_context_scope(
                tenant_id=tenant_id,
                canonical_id=ctx_id,
                repo_identifiers=None,
                is_locked=False,
                changed_by="alice@acme.example",
                scope_rows=[
                    {"scope_type": "team", "scope_value": "backend"},
                    {"scope_type": "repo", "scope_value": "acme/service-a"},
                    {"scope_type": "path", "scope_value": "src/auth.py"},
                ],
            )

        async with engine.begin() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT scope_type, priority FROM context_repo_scopes "
                            "WHERE canonical_id = :cid"
                        ),
                        {"cid": ctx_id},
                    )
                )
                .mappings()
                .fetchall()
            )
        priority_by_type = {r["scope_type"]: r["priority"] for r in rows}
        assert priority_by_type == {"team": 1, "repo": 2, "path": 3}
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_repo_scopes"))
            await conn.execute(text("DELETE FROM context_scope_audit_log"))
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_scope_filter_matches_team_tier() -> None:
    """A context scoped to team "backend" is eligible for a request naming
    that team, and ineligible for a different team -- same never-silently-
    everything discipline as the repo tier."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    embedding = [0.2] * 1024

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Backend team's on-call rotation rule",
                confidence_score=5.0,
                embedding=embedding,
            )
            await store.update_context_scope(
                tenant_id=tenant_id,
                canonical_id=ctx_id,
                repo_identifiers=None,
                is_locked=False,
                changed_by="alice@acme.example",
                scope_rows=[{"scope_type": "team", "scope_value": "backend"}],
            )

            matching = await store.find_similar_contexts(
                tenant_id, embedding, threshold=0.5, team_identifier="backend"
            )
            assert {r["id"] for r in matching} == {str(ctx_id)}

            non_matching = await store.find_similar_contexts(
                tenant_id, embedding, threshold=0.5, team_identifier="frontend"
            )
            assert {r["id"] for r in non_matching} == set()

            no_scope_given = await store.find_similar_contexts(tenant_id, embedding, threshold=0.5)
            assert {r["id"] for r in no_scope_given} == set()
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_repo_scopes"))
            await conn.execute(text("DELETE FROM context_scope_audit_log"))
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_scope_filter_matches_path_tier() -> None:
    """A context scoped to a path is eligible for a request naming that
    exact path, and ineligible for a different one."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    embedding = [0.3] * 1024

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Auth module handling rule",
                confidence_score=5.0,
                embedding=embedding,
            )
            await store.update_context_scope(
                tenant_id=tenant_id,
                canonical_id=ctx_id,
                repo_identifiers=None,
                is_locked=False,
                changed_by="alice@acme.example",
                scope_rows=[{"scope_type": "path", "scope_value": "src/auth.py"}],
            )

            matching = await store.find_similar_contexts(
                tenant_id, embedding, threshold=0.5, path="src/auth.py"
            )
            assert {r["id"] for r in matching} == {str(ctx_id)}

            non_matching = await store.find_similar_contexts(
                tenant_id, embedding, threshold=0.5, path="src/other.py"
            )
            assert {r["id"] for r in non_matching} == set()
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_repo_scopes"))
            await conn.execute(text("DELETE FROM context_scope_audit_log"))
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_scope_filter_org_global_still_matches_every_tier_query() -> None:
    """Org-global (zero scope rows) remains eligible for any combination of
    repo/team/path identifiers -- generalizing to more tiers must not
    accidentally narrow the existing org-global-always-eligible guarantee."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    embedding = [0.4] * 1024

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Org-wide rule",
                confidence_score=5.0,
                embedding=embedding,
            )

            results = await store.find_similar_contexts(
                tenant_id,
                embedding,
                threshold=0.5,
                repo_identifier="acme/service-a",
                team_identifier="backend",
                path="src/auth.py",
            )
            assert {r["summary"] for r in results} == {"Org-wide rule"}
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_update_context_scope_with_typed_scope_rows() -> None:
    """scope_rows accepts a typed, multi-tier set in one call."""
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Multi-tier scoped context",
                confidence_score=5.0,
            )
            updated = await store.update_context_scope(
                tenant_id=tenant_id,
                canonical_id=ctx_id,
                repo_identifiers=None,
                is_locked=False,
                changed_by="alice@acme.example",
                scope_rows=[
                    {"scope_type": "team", "scope_value": "backend"},
                    {"scope_type": "repo", "scope_value": "acme/service-a"},
                    {"scope_type": "path", "scope_value": "src/auth.py"},
                ],
            )
            assert updated is not None
            assert sorted(updated["scope_rows"], key=lambda r: r["scope_type"]) == [
                {"scope_type": "path", "scope_value": "src/auth.py"},
                {"scope_type": "repo", "scope_value": "acme/service-a"},
                {"scope_type": "team", "scope_value": "backend"},
            ]
            # The repo-only accessor/response field sees only the repo-tier
            # subset.
            assert updated["scope_repo_identifiers"] == ["acme/service-a"]
            assert _repo_scopes(await store.get_context_scope_rows(tenant_id, ctx_id)) == [
                "acme/service-a"
            ]

            full_rows = await store.get_context_scope_rows(tenant_id, ctx_id)
            assert sorted(full_rows, key=lambda r: r["scope_type"]) == [
                {"scope_type": "path", "scope_value": "src/auth.py"},
                {"scope_type": "repo", "scope_value": "acme/service-a"},
                {"scope_type": "team", "scope_value": "backend"},
            ]
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_repo_scopes"))
            await conn.execute(text("DELETE FROM context_scope_audit_log"))
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_update_context_scope_audit_log_records_typed_rows() -> None:
    """previous_scope_rows/new_scope_rows capture the full typed diff,
    independent of the repo-only previous_scope_repo_identifiers/
    new_scope_repo_identifiers columns.
    """
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Context re-scoped with typed rows",
                confidence_score=5.0,
            )
            await store.update_context_scope(
                tenant_id=tenant_id,
                canonical_id=ctx_id,
                repo_identifiers=None,
                is_locked=False,
                changed_by="alice@acme.example",
                scope_rows=[{"scope_type": "team", "scope_value": "backend"}],
            )

        async with engine.begin() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT previous_scope_rows, new_scope_rows, "
                            "previous_scope_repo_identifiers, new_scope_repo_identifiers "
                            "FROM context_scope_audit_log WHERE canonical_id = :cid"
                        ),
                        {"cid": ctx_id},
                    )
                )
                .mappings()
                .fetchall()
            )
        assert len(rows) == 1
        audit = rows[0]
        assert audit["previous_scope_rows"] is None
        assert audit["new_scope_rows"] == [{"scope_type": "team", "scope_value": "backend"}]
        # Repo-only columns stay None -- a team-only change has no repo-tier
        # rows at all.
        assert audit["previous_scope_repo_identifiers"] is None
        assert audit["new_scope_repo_identifiers"] is None
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_repo_scopes"))
            await conn.execute(text("DELETE FROM context_scope_audit_log"))
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_get_latest_scope_change_returns_none_when_never_edited() -> None:
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id, summary="Never edited", confidence_score=1.0
            )
            assert await store.get_latest_scope_change(tenant_id, ctx_id) is None
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_get_latest_scope_change_returns_the_most_recent_entry() -> None:
    """Two sequential edits: the history accessor returns only the latest
    changed_by.
    """
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id, summary="Edited twice", confidence_score=1.0
            )
            await store.update_context_scope(
                tenant_id=tenant_id,
                canonical_id=ctx_id,
                repo_identifiers=["acme/service-a"],
                is_locked=False,
                changed_by="alice@acme.example",
            )
            await store.update_context_scope(
                tenant_id=tenant_id,
                canonical_id=ctx_id,
                repo_identifiers=["acme/service-b"],
                is_locked=True,
                changed_by="bob@acme.example",
            )

            latest = await store.get_latest_scope_change(tenant_id, ctx_id)
            assert latest is not None
            assert latest["changed_by"] == "bob@acme.example"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_repo_scopes"))
            await conn.execute(text("DELETE FROM context_scope_audit_log"))
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_ast_drift_independent_per_repo_for_a_multi_scoped_context() -> None:
    """A context scoped to both service-a and service-b, with an ACTIVE edge
    into service-a and a STALE edge into service-b, surfaces for service-a
    queries and stays suppressed for service-b queries: the drift check is
    keyed on (canonical_id, repo).
    """
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    repo_a = "acme/service-a"
    repo_b = "acme/service-b"

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            ctx_id = await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="Code-linked rule shared across two repos",
                confidence_score=9.0,
            )
            await store.update_context_scope(
                tenant_id=tenant_id,
                canonical_id=ctx_id,
                repo_identifiers=[repo_a, repo_b],
                is_locked=False,
                changed_by="alice@acme.example",
            )

            # ACTIVE edge into repo_a.
            await store.upsert_ast_node_and_check_drift(
                tenant_id=tenant_id,
                repo_identifier=repo_a,
                file_path="src/a.py",
                symbol_name="do_thing_a",
                node_type="function",
                merkle_hash="hash_a_v1",
            )
            nodes_a = await store.find_ast_nodes_by_symbols(tenant_id, ["do_thing_a"])
            await store.link_ast_to_context(tenant_id, nodes_a[0]["id"], ctx_id)

            # Edge into repo_b that will be made STALE.
            await store.upsert_ast_node_and_check_drift(
                tenant_id=tenant_id,
                repo_identifier=repo_b,
                file_path="src/b.py",
                symbol_name="do_thing_b",
                node_type="function",
                merkle_hash="hash_b_v1",
            )
            nodes_b = await store.find_ast_nodes_by_symbols(tenant_id, ["do_thing_b"])
            await store.link_ast_to_context(tenant_id, nodes_b[0]["id"], ctx_id)
            # Trigger drift on the repo_b edge only.
            await store.upsert_ast_node_and_check_drift(
                tenant_id=tenant_id,
                repo_identifier=repo_b,
                file_path="src/b.py",
                symbol_name="do_thing_b",
                node_type="function",
                merkle_hash="hash_b_v2",
            )

            results_a = await store.get_top_contexts_for_repo(tenant_id, repo_a)
            results_b = await store.get_top_contexts_for_repo(tenant_id, repo_b)

            assert {r["id"] for r in results_a} == {str(ctx_id)}
            assert {r["id"] for r in results_b} == set()
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM ast_context_edges"))
            await conn.execute(text("DELETE FROM ast_nodes"))
            await conn.execute(text("DELETE FROM context_repo_scopes"))
            await conn.execute(text("DELETE FROM context_scope_audit_log"))
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()
