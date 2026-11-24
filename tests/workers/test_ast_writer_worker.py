from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from checkcord.models.pr_merge import PRMergedPayload
from checkcord.workers.ast_writer_worker import ASTWriterWorker


@pytest.fixture(autouse=True)
def token_lookup(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    lookup = AsyncMock(return_value="token")
    monkeypatch.setattr("checkcord.workers.ast_writer_worker.github_token_for", lookup)
    return lookup


@pytest.mark.asyncio
@patch("checkcord.workers.ast_writer_worker.write_ast_nodes_for_file")
@patch("checkcord.workers.ast_writer_worker.GitHubRestClient")
@patch("checkcord.workers.ast_writer_worker.tenant_context")
@patch("checkcord.workers.ast_writer_worker.PostgresEventStore")
async def test_process_message_writes_all_changed_files(
    MockStore, mock_tenant_context, MockGitHubRestClient, mock_write
) -> None:
    tenant_id = uuid4()

    @asynccontextmanager
    async def _ctx(engine: object, tid: object) -> object:
        yield MagicMock()

    mock_tenant_context.side_effect = _ctx

    client_mock = MockGitHubRestClient.return_value
    client_mock.get_pull_request_files = AsyncMock(return_value=(["src/a.py", "src/b.py"], 10, 2))
    client_mock.get_file_content = AsyncMock(return_value=b"def f():\n    pass\n")

    from checkcord.services.ast_writer import WriteResult

    mock_write.return_value = WriteResult(file_path="x", nodes_written=1)

    worker = ASTWriterWorker(bus=AsyncMock(), engine=MagicMock())
    payload = PRMergedPayload(
        tenant_id=tenant_id,
        repository_full_name="owner/repo",
        pull_request_number=42,
        merge_commit_sha="merge_sha_1",
    )

    await worker.process_message(payload)

    assert mock_write.call_count == 2
    for call in mock_write.call_args_list:
        assert call.kwargs["tenant_id"] == tenant_id
        assert call.kwargs["repo_identifier"] == "owner/repo"

    # Content must be fetched at the merge commit SHA, not a head SHA.
    for call in client_mock.get_file_content.call_args_list:
        assert call.args[-1] == "merge_sha_1"


@pytest.mark.asyncio
@patch("checkcord.workers.ast_writer_worker.GitHubRestClient")
async def test_process_message_no_files_changed_is_a_noop(MockGitHubRestClient) -> None:
    client_mock = MockGitHubRestClient.return_value
    client_mock.get_pull_request_files = AsyncMock(return_value=([], 0, 0))

    worker = ASTWriterWorker(bus=AsyncMock(), engine=MagicMock())
    payload = PRMergedPayload(
        tenant_id=uuid4(),
        repository_full_name="owner/repo",
        pull_request_number=42,
        merge_commit_sha="merge_sha_1",
    )

    # Must return cleanly without ever needing a DB connection.
    await worker.process_message(payload)


@pytest.mark.asyncio
@patch("checkcord.workers.ast_writer_worker.write_ast_nodes_for_file")
@patch("checkcord.workers.ast_writer_worker.GitHubRestClient")
@patch("checkcord.workers.ast_writer_worker.tenant_context")
@patch("checkcord.workers.ast_writer_worker.PostgresEventStore")
async def test_process_message_skips_files_with_no_content(
    MockStore, mock_tenant_context, MockGitHubRestClient, mock_write
) -> None:
    """A file fetch failure/404 for one changed file must not crash the
    whole merge event -- matches ast_writer's own per-file skip semantics."""

    @asynccontextmanager
    async def _ctx(engine: object, tid: object) -> object:
        yield MagicMock()

    mock_tenant_context.side_effect = _ctx

    client_mock = MockGitHubRestClient.return_value
    client_mock.get_pull_request_files = AsyncMock(
        return_value=(["src/deleted.py", "src/kept.py"], 0, 0)
    )

    async def _get_file_content(owner: str, repo: str, path: str, ref: str) -> bytes | None:
        if path == "src/deleted.py":
            return None
        return b"def f():\n    pass\n"

    client_mock.get_file_content = AsyncMock(side_effect=_get_file_content)

    from checkcord.services.ast_writer import WriteResult

    mock_write.return_value = WriteResult(file_path="x", nodes_written=1)

    worker = ASTWriterWorker(bus=AsyncMock(), engine=MagicMock())
    payload = PRMergedPayload(
        tenant_id=uuid4(),
        repository_full_name="owner/repo",
        pull_request_number=42,
        merge_commit_sha="merge_sha_1",
    )

    await worker.process_message(payload)

    # Only the file with real content is passed to the shared writer.
    assert mock_write.call_count == 1
    assert mock_write.call_args.kwargs["file_path"] == "src/kept.py"


@pytest.mark.asyncio
@patch("checkcord.workers.ast_writer_worker.GitHubRestClient")
@patch("checkcord.workers.ast_writer_worker.tenant_context")
async def test_process_message_propagates_write_failure_for_dlq(
    mock_tenant_context, MockGitHubRestClient
) -> None:
    """An unexpected failure while writing must propagate so the bus's
    retry/DLQ machinery handles it.
    """

    @asynccontextmanager
    async def _ctx(engine: object, tid: object) -> object:
        yield MagicMock()

    mock_tenant_context.side_effect = _ctx

    client_mock = MockGitHubRestClient.return_value
    client_mock.get_pull_request_files = AsyncMock(return_value=(["src/a.py"], 0, 0))
    client_mock.get_file_content = AsyncMock(side_effect=RuntimeError("db exploded"))

    worker = ASTWriterWorker(bus=AsyncMock(), engine=MagicMock())
    payload = PRMergedPayload(
        tenant_id=uuid4(),
        repository_full_name="owner/repo",
        pull_request_number=42,
        merge_commit_sha="merge_sha_1",
    )

    with pytest.raises(RuntimeError):
        await worker.process_message(payload)


@pytest.mark.asyncio
@patch("checkcord.workers.ast_writer_worker.GitHubRestClient")
async def test_token_comes_from_lookup_and_legacy_field_is_ignored(
    MockGitHubRestClient, token_lookup
) -> None:
    MockGitHubRestClient.return_value.get_pull_request_files = AsyncMock(return_value=([], 0, 0))
    tenant_id = uuid4()
    worker = ASTWriterWorker(bus=AsyncMock(), engine=MagicMock())
    payload = PRMergedPayload.model_validate_json(
        PRMergedPayload(
            tenant_id=tenant_id,
            repository_full_name="owner/repo",
            pull_request_number=42,
            merge_commit_sha="m",
        )
        .model_dump_json()
        .replace("{", '{"personal_access_token": "ghp_stale_in_flight", ', 1)
    )

    await worker.process_message(payload)

    assert "ghp_stale_in_flight" not in payload.model_dump_json()
    token_lookup.assert_awaited_once_with(worker.engine, tenant_id, "owner/repo")
    MockGitHubRestClient.assert_called_once_with("token")
