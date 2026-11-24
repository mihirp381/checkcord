from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from checkcord.core.exceptions import GitHubAPIError
from checkcord.services.ci_failure_linker import link_ci_failure_to_contexts
from checkcord.storage.postgres import PostgresEventStore

pytestmark = pytest.mark.asyncio


@patch("checkcord.services.ci_failure_linker.GitHubRestClient")
async def test_link_ci_failure_to_contexts_happy_path(MockClient) -> None:
    tenant_id = uuid4()
    ci_event_id = uuid4()
    canonical_id_1 = uuid4()
    canonical_id_2 = uuid4()
    ast_node_id_1 = uuid4()

    client_mock = MockClient.return_value
    client_mock.list_pulls_associated_with_commit = AsyncMock(
        return_value=[{"number": 42, "updated_at": "2026-08-01T00:00:00Z"}]
    )
    client_mock.get_pull_request_files = AsyncMock(return_value=(["src/a.py", "src/b.py"], 10, 2))

    store = AsyncMock(spec=PostgresEventStore)
    store.find_ast_nodes_by_paths.return_value = [
        {
            "ast_node_id": ast_node_id_1,
            "file_path": "src/a.py",
            "symbol_name": "foo",
            "node_type": "function_definition",
            "canonical_id": canonical_id_1,
        },
        {
            "ast_node_id": uuid4(),
            "file_path": "src/b.py",
            "symbol_name": "bar",
            "node_type": "function_definition",
            "canonical_id": canonical_id_2,
        },
        {
            # No active edge for this symbol -- must be skipped, not linked.
            "ast_node_id": uuid4(),
            "file_path": "src/a.py",
            "symbol_name": "unlinked",
            "node_type": "function_definition",
            "canonical_id": None,
        },
    ]
    store.create_ci_failure_context_link.return_value = True

    count = await link_ci_failure_to_contexts(
        tenant_id=tenant_id,
        repo_identifier="owner/repo",
        commit_sha="sha123",
        ci_event_id=ci_event_id,
        pat="pat",
        store=store,
    )

    assert count == 2
    assert store.create_ci_failure_context_link.call_count == 2
    calls = store.create_ci_failure_context_link.call_args_list
    linked_canonical_ids = {c.kwargs["canonical_id"] for c in calls}
    assert linked_canonical_ids == {canonical_id_1, canonical_id_2}
    for c in calls:
        assert c.kwargs["tenant_id"] == tenant_id
        assert c.kwargs["ci_event_id"] == ci_event_id


@patch("checkcord.services.ci_failure_linker.GitHubRestClient")
async def test_link_ci_failure_to_contexts_narrows_to_extracted_file_path(MockClient) -> None:
    """When the extractor reported a specific file path that IS one of the
    PR's changed files, linking should be scoped to just that file."""
    tenant_id = uuid4()
    ci_event_id = uuid4()
    canonical_id = uuid4()

    client_mock = MockClient.return_value
    client_mock.list_pulls_associated_with_commit = AsyncMock(
        return_value=[{"number": 42, "updated_at": "2026-08-01T00:00:00Z"}]
    )
    client_mock.get_pull_request_files = AsyncMock(return_value=(["src/a.py", "src/b.py"], 0, 0))

    store = AsyncMock(spec=PostgresEventStore)
    store.find_ast_nodes_by_paths.return_value = [
        {
            "ast_node_id": uuid4(),
            "file_path": "src/a.py",
            "symbol_name": "foo",
            "node_type": "function_definition",
            "canonical_id": canonical_id,
        },
    ]
    store.create_ci_failure_context_link.return_value = True

    await link_ci_failure_to_contexts(
        tenant_id=tenant_id,
        repo_identifier="owner/repo",
        commit_sha="sha123",
        ci_event_id=ci_event_id,
        pat="pat",
        store=store,
        extracted_file_path="src/a.py",
    )

    store.find_ast_nodes_by_paths.assert_called_once_with(
        tenant_id=tenant_id, repo_identifier="owner/repo", file_paths=["src/a.py"]
    )


@patch("checkcord.services.ci_failure_linker.GitHubRestClient")
async def test_link_ci_failure_to_contexts_no_pr_resolves_gracefully(MockClient) -> None:
    """No PR associated with the commit (direct push, scheduled run): log and
    return 0, never raise.
    """
    client_mock = MockClient.return_value
    client_mock.list_pulls_associated_with_commit = AsyncMock(return_value=[])

    store = AsyncMock(spec=PostgresEventStore)

    count = await link_ci_failure_to_contexts(
        tenant_id=uuid4(),
        repo_identifier="owner/repo",
        commit_sha="sha123",
        ci_event_id=uuid4(),
        pat="pat",
        store=store,
    )

    assert count == 0
    store.find_ast_nodes_by_paths.assert_not_called()


@patch("checkcord.services.ci_failure_linker.GitHubRestClient")
async def test_link_ci_failure_to_contexts_pr_resolution_failure_gracefully(MockClient) -> None:
    """A GitHubAPIError resolving the PR must not raise."""
    client_mock = MockClient.return_value
    client_mock.list_pulls_associated_with_commit = AsyncMock(side_effect=GitHubAPIError("boom"))

    store = AsyncMock(spec=PostgresEventStore)

    count = await link_ci_failure_to_contexts(
        tenant_id=uuid4(),
        repo_identifier="owner/repo",
        commit_sha="sha123",
        ci_event_id=uuid4(),
        pat="pat",
        store=store,
    )

    assert count == 0


@patch("checkcord.services.ci_failure_linker.GitHubRestClient")
async def test_link_ci_failure_to_contexts_pr_has_no_files(MockClient) -> None:
    client_mock = MockClient.return_value
    client_mock.list_pulls_associated_with_commit = AsyncMock(
        return_value=[{"number": 42, "updated_at": "2026-08-01T00:00:00Z"}]
    )
    client_mock.get_pull_request_files = AsyncMock(return_value=([], 0, 0))

    store = AsyncMock(spec=PostgresEventStore)

    count = await link_ci_failure_to_contexts(
        tenant_id=uuid4(),
        repo_identifier="owner/repo",
        commit_sha="sha123",
        ci_event_id=uuid4(),
        pat="pat",
        store=store,
    )

    assert count == 0
    store.find_ast_nodes_by_paths.assert_not_called()


async def test_link_ci_failure_to_contexts_malformed_repo_identifier() -> None:
    store = AsyncMock(spec=PostgresEventStore)

    count = await link_ci_failure_to_contexts(
        tenant_id=uuid4(),
        repo_identifier="not-a-valid-repo-identifier",
        commit_sha="sha123",
        ci_event_id=uuid4(),
        pat="pat",
        store=store,
    )

    assert count == 0


@patch("checkcord.services.ci_failure_linker.GitHubRestClient")
async def test_link_ci_failure_to_contexts_no_ast_nodes_yet(MockClient) -> None:
    """No ast_nodes rows exist yet for the affected files (e.g. backfill
    hasn't run) -- must return 0, not error."""
    client_mock = MockClient.return_value
    client_mock.list_pulls_associated_with_commit = AsyncMock(
        return_value=[{"number": 42, "updated_at": "2026-08-01T00:00:00Z"}]
    )
    client_mock.get_pull_request_files = AsyncMock(return_value=(["src/a.py"], 0, 0))

    store = AsyncMock(spec=PostgresEventStore)
    store.find_ast_nodes_by_paths.return_value = []

    count = await link_ci_failure_to_contexts(
        tenant_id=uuid4(),
        repo_identifier="owner/repo",
        commit_sha="sha123",
        ci_event_id=uuid4(),
        pat="pat",
        store=store,
    )

    assert count == 0


@patch("checkcord.services.ci_failure_linker.GitHubRestClient")
async def test_link_ci_failure_to_contexts_picks_most_recently_updated_pr(MockClient) -> None:
    """Multiple PRs associated with one commit -- must use the most recently
    updated one."""
    client_mock = MockClient.return_value
    client_mock.list_pulls_associated_with_commit = AsyncMock(
        return_value=[
            {"number": 1, "updated_at": "2026-01-01T00:00:00Z"},
            {"number": 99, "updated_at": "2026-08-01T00:00:00Z"},
        ]
    )
    client_mock.get_pull_request_files = AsyncMock(return_value=([], 0, 0))

    store = AsyncMock(spec=PostgresEventStore)

    await link_ci_failure_to_contexts(
        tenant_id=uuid4(),
        repo_identifier="owner/repo",
        commit_sha="sha123",
        ci_event_id=uuid4(),
        pat="pat",
        store=store,
    )

    client_mock.get_pull_request_files.assert_called_once_with("owner", "repo", 99)
