from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from checkcord.core.exceptions import GitHubAPIError, IngestionError
from checkcord.models.repo_backfill import RepoBackfillRequestPayload
from checkcord.workers.repo_backfill import RepoBackfillWorker, _cursor_key


@pytest.fixture(autouse=True)
def token_lookup(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    lookup = AsyncMock(return_value="fake_pat")
    monkeypatch.setattr("checkcord.workers.repo_backfill.github_token_for", lookup)
    return lookup


def _mock_tenant_context() -> AsyncMock:
    @asynccontextmanager
    async def _ctx(engine: object, tenant_id: object) -> object:
        yield MagicMock()

    return _ctx


@pytest.mark.asyncio
@patch("checkcord.workers.repo_backfill.write_ast_nodes_for_file")
@patch("checkcord.workers.repo_backfill.GitHubRestClient")
@patch("checkcord.workers.repo_backfill.tenant_context")
@patch("checkcord.workers.repo_backfill.PostgresEventStore")
async def test_process_message_full_backfill(
    MockStore, mock_tenant_context, MockGitHubRestClient, mock_write, token_lookup
) -> None:
    tenant_id = uuid4()
    repo_identifier = "owner/repo"
    mock_tenant_context.side_effect = _mock_tenant_context()

    client_mock = MockGitHubRestClient.return_value
    client_mock.get_repository = AsyncMock(return_value={"default_branch": "main"})
    client_mock.list_repository_tree = AsyncMock(
        return_value=[
            {"path": "src/main.py", "type": "blob"},
            {"path": "src/utils.py", "type": "blob"},
            {"path": "README.md", "type": "blob"},  # unsupported language, filtered out
            {"path": "node_modules/lib.js", "type": "blob"},  # excluded path, filtered out
            {"path": "src", "type": "tree"},  # a directory, not a blob
        ]
    )
    client_mock.get_file_content = AsyncMock(return_value=b"def f():\n    pass\n")

    from checkcord.services.ast_writer import WriteResult

    mock_write.return_value = WriteResult(file_path="x", nodes_written=1)

    redis_mock = AsyncMock()
    redis_mock.get.return_value = None  # no existing cursor

    worker = RepoBackfillWorker(bus=AsyncMock(), engine=MagicMock(), redis_client=redis_mock)

    payload = RepoBackfillRequestPayload(tenant_id=tenant_id, repo_identifier=repo_identifier)
    await worker.process_message(payload)
    token_lookup.assert_awaited_once_with(worker.engine, tenant_id, repo_identifier)
    MockGitHubRestClient.assert_called_once_with("fake_pat")

    # Only the two python files should have been written.
    assert mock_write.call_count == 2
    written_paths = {call.kwargs["file_path"] for call in mock_write.call_args_list}
    assert written_paths == {"src/main.py", "src/utils.py"}

    # Cursor cleared on completion.
    redis_mock.delete.assert_called_with(_cursor_key(tenant_id, repo_identifier))


@pytest.mark.asyncio
async def test_process_message_no_installation_raises(token_lookup: AsyncMock) -> None:
    token_lookup.side_effect = LookupError("no stored GitHub token for owner/repo")
    worker = RepoBackfillWorker(bus=AsyncMock(), engine=MagicMock(), redis_client=AsyncMock())
    payload = RepoBackfillRequestPayload(tenant_id=uuid4(), repo_identifier="owner/repo")

    with pytest.raises(IngestionError):
        await worker.process_message(payload)


@pytest.mark.asyncio
@patch("checkcord.workers.repo_backfill.GitHubRestClient")
@patch("checkcord.workers.repo_backfill.tenant_context")
async def test_process_message_no_default_branch_raises(
    mock_tenant_context, MockGitHubRestClient
) -> None:
    mock_tenant_context.side_effect = _mock_tenant_context()

    client_mock = MockGitHubRestClient.return_value
    client_mock.get_repository = AsyncMock(return_value=None)

    worker = RepoBackfillWorker(bus=AsyncMock(), engine=MagicMock(), redis_client=AsyncMock())
    payload = RepoBackfillRequestPayload(tenant_id=uuid4(), repo_identifier="owner/repo")

    with pytest.raises(IngestionError):
        await worker.process_message(payload)


@pytest.mark.asyncio
@patch("checkcord.workers.repo_backfill.GitHubRestClient")
@patch("checkcord.workers.repo_backfill.tenant_context")
async def test_process_message_tree_listing_failure_propagates(
    mock_tenant_context, MockGitHubRestClient
) -> None:
    """A failed tree listing must propagate (triggering the bus's retry/DLQ),
    not be silently treated as an empty repo (M15's whole point)."""
    mock_tenant_context.side_effect = _mock_tenant_context()

    client_mock = MockGitHubRestClient.return_value
    client_mock.get_repository = AsyncMock(return_value={"default_branch": "main"})
    client_mock.list_repository_tree = AsyncMock(
        side_effect=GitHubAPIError("boom", status_code=500)
    )

    worker = RepoBackfillWorker(bus=AsyncMock(), engine=MagicMock(), redis_client=AsyncMock())
    payload = RepoBackfillRequestPayload(tenant_id=uuid4(), repo_identifier="owner/repo")

    with pytest.raises(GitHubAPIError):
        await worker.process_message(payload)


@pytest.mark.asyncio
@patch("checkcord.workers.repo_backfill.write_ast_nodes_for_file")
@patch("checkcord.workers.repo_backfill.GitHubRestClient")
@patch("checkcord.workers.repo_backfill.tenant_context")
async def test_process_message_resumes_from_cursor(
    mock_tenant_context, MockGitHubRestClient, mock_write
) -> None:
    """A worker restarting mid-repo must resume from the persisted cursor,
    not re-process already-completed files.
    """
    tenant_id = uuid4()
    repo_identifier = "owner/repo"

    mock_tenant_context.side_effect = _mock_tenant_context()

    client_mock = MockGitHubRestClient.return_value
    client_mock.get_repository = AsyncMock(return_value={"default_branch": "main"})
    # Three supported files, sorted: a.py, b.py, c.py
    client_mock.list_repository_tree = AsyncMock(
        return_value=[
            {"path": "a.py", "type": "blob"},
            {"path": "b.py", "type": "blob"},
            {"path": "c.py", "type": "blob"},
        ]
    )
    client_mock.get_file_content = AsyncMock(return_value=b"def f():\n    pass\n")

    from checkcord.services.ast_writer import WriteResult

    mock_write.return_value = WriteResult(file_path="x", nodes_written=1)

    redis_mock = AsyncMock()
    # Cursor says the first file (index 0..1, i.e. "a.py") is already done.
    redis_mock.get.return_value = b"1"

    worker = RepoBackfillWorker(bus=AsyncMock(), engine=MagicMock(), redis_client=redis_mock)
    payload = RepoBackfillRequestPayload(tenant_id=tenant_id, repo_identifier=repo_identifier)

    await worker.process_message(payload)

    written_paths = {call.kwargs["file_path"] for call in mock_write.call_args_list}
    assert written_paths == {"b.py", "c.py"}
    assert "a.py" not in written_paths


@pytest.mark.asyncio
@patch("checkcord.workers.repo_backfill.GitHubRestClient")
@patch("checkcord.workers.repo_backfill.tenant_context")
async def test_process_message_excludes_vendored_and_unsupported_paths(
    mock_tenant_context, MockGitHubRestClient
) -> None:
    mock_tenant_context.side_effect = _mock_tenant_context()

    client_mock = MockGitHubRestClient.return_value
    client_mock.get_repository = AsyncMock(return_value={"default_branch": "main"})
    client_mock.list_repository_tree = AsyncMock(
        return_value=[
            {"path": "vendor/lib.py", "type": "blob"},
            {"path": "dist/out.py", "type": "blob"},
            {"path": "image.png", "type": "blob"},
        ]
    )

    redis_mock = AsyncMock()
    redis_mock.get.return_value = None

    worker = RepoBackfillWorker(bus=AsyncMock(), engine=MagicMock(), redis_client=redis_mock)
    payload = RepoBackfillRequestPayload(tenant_id=uuid4(), repo_identifier="owner/repo")

    await worker.process_message(payload)

    mock_tenant_context.assert_not_called()
    redis_mock.delete.assert_called_once()
