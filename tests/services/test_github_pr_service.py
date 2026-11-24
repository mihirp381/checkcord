from unittest.mock import AsyncMock, patch

import pytest

from checkcord.services.github_pr_service import GitHubPRService

pytestmark = pytest.mark.asyncio


async def test_github_pr_service_missing_token() -> None:
    service = GitHubPRService()

    with patch("checkcord.services.github_pr_service.GitHubRestClient") as client:
        result = await service.propose_context_update(
            repo_full_name="my-org/my-repo",
            architecture_md="Some architecture markdown",
            ledger_json="{}",
            pat="",
        )
    assert result is False
    client.assert_not_called()


async def test_github_pr_service_no_changes() -> None:
    service = GitHubPRService()

    mock_client = AsyncMock()
    mock_client.get_repository.return_value = {"default_branch": "main"}
    mock_client.get_ref.side_effect = lambda owner, repo, ref: (
        "base_sha" if ref == "heads/main" else "branch_sha"
    )
    mock_client.get_file_info.side_effect = lambda owner, repo, path, ref: ({"sha": f"{path}_sha"})
    mock_client.get_file_content.side_effect = lambda owner, repo, path, ref: (
        b"Some architecture markdown" if path == ".context/ARCHITECTURE.md" else b"{}"
    )

    with patch("checkcord.services.github_pr_service.GitHubRestClient", return_value=mock_client):
        result = await service.propose_context_update(
            repo_full_name="my-org/my-repo",
            architecture_md="Some architecture markdown",
            ledger_json="{}",
            pat="fake_pat",
        )
        assert result is True
        assert mock_client.create_or_update_file.call_count == 0
        assert mock_client.create_pull_request.call_count == 0


async def test_github_pr_service_new_branch_and_pr() -> None:
    service = GitHubPRService()

    mock_client = AsyncMock()
    mock_client.get_repository.return_value = {"default_branch": "main"}
    mock_client.get_ref.side_effect = lambda owner, repo, ref: (
        "base_sha" if ref == "heads/main" else None
    )
    mock_client.create_ref.return_value = True
    mock_client.get_file_info.return_value = None
    mock_client.create_or_update_file.return_value = "new_commit_sha"
    mock_client.list_pull_requests.return_value = []
    mock_client.create_pull_request.return_value = {"number": 123, "html_url": "http://pr-url"}

    with patch("checkcord.services.github_pr_service.GitHubRestClient", return_value=mock_client):
        result = await service.propose_context_update(
            repo_full_name="my-org/my-repo",
            architecture_md="Some architecture markdown",
            ledger_json="{}",
            pat="fake_pat",
        )
        assert result is True
        mock_client.create_ref.assert_called_once_with(
            "my-org", "my-repo", "heads/checkcord/update-context", "base_sha"
        )
        assert mock_client.create_or_update_file.call_count == 2
        mock_client.create_pull_request.assert_called_once()


async def test_github_pr_service_existing_branch_and_pr() -> None:
    service = GitHubPRService()

    mock_client = AsyncMock()
    mock_client.get_repository.return_value = {"default_branch": "main"}
    mock_client.get_ref.side_effect = lambda owner, repo, ref: (
        "base_sha" if ref == "heads/main" else "branch_sha"
    )
    mock_client.get_file_info.side_effect = lambda owner, repo, path, ref: ({"sha": f"{path}_sha"})
    mock_client.get_file_content.side_effect = lambda owner, repo, path, ref: (
        b"Old doc" if path == ".context/ARCHITECTURE.md" else b"{}"
    )
    mock_client.create_or_update_file.return_value = "new_commit_sha"

    mock_client.list_pull_requests.return_value = [
        {
            "number": 123,
            "html_url": "http://pr-url",
            "head": {"ref": "checkcord/update-context"},
        }
    ]

    with patch("checkcord.services.github_pr_service.GitHubRestClient", return_value=mock_client):
        result = await service.propose_context_update(
            repo_full_name="my-org/my-repo",
            architecture_md="New doc",
            ledger_json="{}",
            pat="fake_pat",
        )
        assert result is True
        mock_client.create_or_update_file.assert_called_once_with(
            owner="my-org",
            repo="my-repo",
            path=".context/ARCHITECTURE.md",
            message="chore: update .context/ARCHITECTURE.md from Checkcord",
            content=b"New doc",
            branch="checkcord/update-context",
            sha=".context/ARCHITECTURE.md_sha",
        )
        assert mock_client.create_pull_request.call_count == 0


async def test_github_pr_service_branch_rebase_on_conflict() -> None:
    """If the branch exists but differs, it must be force-updated to default_branch_sha."""
    service = GitHubPRService()

    mock_client = AsyncMock()
    mock_client.get_repository.return_value = {"default_branch": "main"}
    mock_client.get_ref.side_effect = lambda owner, repo, ref: (
        "base_sha" if ref == "heads/main" else "branch_sha"
    )
    # File info returns sha only for default branch contents
    mock_client.get_file_info.side_effect = lambda owner, repo, path, ref: (
        {"sha": f"{path}_sha"} if ref in ("main", "heads/main") else None
    )
    # File content on default branch is Old, but on PR branch it is also different/None
    mock_client.get_file_content.side_effect = lambda owner, repo, path, ref: (
        b"Old doc" if ref == "main" else b"Outdated PR content"
    )
    mock_client.update_ref.return_value = True
    mock_client.create_or_update_file.return_value = "new_commit_sha"
    mock_client.list_pull_requests.return_value = []
    mock_client.create_pull_request.return_value = {"number": 124, "html_url": "http://pr-url"}

    with patch("checkcord.services.github_pr_service.GitHubRestClient", return_value=mock_client):
        result = await service.propose_context_update(
            repo_full_name="my-org/my-repo",
            architecture_md="New doc",
            ledger_json="{}",
            pat="fake_pat",
        )
        assert result is True
        mock_client.update_ref.assert_called_once_with(
            "my-org", "my-repo", "heads/checkcord/update-context", "base_sha", force=True
        )
        assert mock_client.create_or_update_file.call_count == 2
        mock_client.create_pull_request.assert_called_once()


async def test_github_pr_service_spam_prevention() -> None:
    """If the branch exists and matches the compiled files exactly, skip rebase and commit
    but ensure PR.
    """
    service = GitHubPRService()

    mock_client = AsyncMock()
    mock_client.get_repository.return_value = {"default_branch": "main"}
    mock_client.get_ref.side_effect = lambda owner, repo, ref: (
        "base_sha" if ref == "heads/main" else "branch_sha"
    )
    # File info is found on both main and branch
    mock_client.get_file_info.side_effect = lambda owner, repo, path, ref: ({"sha": f"{path}_sha"})

    # Content: main has "Old doc", branch already has "New doc" and "{}"
    def get_content_mock(owner, repo, path, ref):
        if ref == "main":
            return b"Old doc" if path == ".context/ARCHITECTURE.md" else b"{}"
        else:
            return b"New doc" if path == ".context/ARCHITECTURE.md" else b"{}"

    mock_client.get_file_content.side_effect = get_content_mock
    mock_client.list_pull_requests.return_value = []
    mock_client.create_pull_request.return_value = {"number": 125, "html_url": "http://pr-url"}

    with patch("checkcord.services.github_pr_service.GitHubRestClient", return_value=mock_client):
        result = await service.propose_context_update(
            repo_full_name="my-org/my-repo",
            architecture_md="New doc",
            ledger_json="{}",
            pat="fake_pat",
        )
        assert result is True
        # Ensure update_ref and create_or_update_file were NOT called (spam prevention!)
        assert mock_client.update_ref.call_count == 0
        assert mock_client.create_or_update_file.call_count == 0
        # Ensure it still verified/created the PR
        mock_client.create_pull_request.assert_called_once()


async def test_nothing_to_write_and_no_existing_files_opens_no_pull_request() -> None:
    mock_client = AsyncMock()
    mock_client.get_repository.return_value = {"default_branch": "main"}
    mock_client.get_ref.side_effect = lambda owner, repo, ref: (
        "base_sha" if ref == "heads/main" else None
    )
    mock_client.get_file_info.return_value = None

    with patch("checkcord.services.github_pr_service.GitHubRestClient", return_value=mock_client):
        result = await GitHubPRService().propose_context_update(
            repo_full_name="my-org/my-repo",
            architecture_md="empty",
            ledger_json="{}",
            pat="fake_pat",
            create_if_absent=False,
        )

    assert result is True
    mock_client.create_ref.assert_not_called()
    mock_client.create_or_update_file.assert_not_called()
    mock_client.create_pull_request.assert_not_called()


async def test_nothing_to_write_still_replaces_files_already_in_the_repository() -> None:
    mock_client = AsyncMock()
    mock_client.get_repository.return_value = {"default_branch": "main"}
    mock_client.get_ref.side_effect = lambda owner, repo, ref: (
        "base_sha" if ref == "heads/main" else None
    )
    mock_client.get_file_info.side_effect = lambda owner, repo, path, ref: {"sha": f"{path}_sha"}
    mock_client.get_file_content.return_value = b"an earlier record"
    mock_client.create_ref.return_value = True
    mock_client.create_or_update_file.return_value = "commit_sha"
    mock_client.list_pull_requests.return_value = []
    mock_client.create_pull_request.return_value = {"number": 1, "html_url": "http://pr-url"}

    with patch("checkcord.services.github_pr_service.GitHubRestClient", return_value=mock_client):
        result = await GitHubPRService().propose_context_update(
            repo_full_name="my-org/my-repo",
            architecture_md="empty",
            ledger_json="{}",
            pat="fake_pat",
            create_if_absent=False,
        )

    assert result is True
    written = {
        call.kwargs["path"]: call.kwargs["content"]
        for call in mock_client.create_or_update_file.call_args_list
    }
    assert written == {".context/ARCHITECTURE.md": b"empty", ".context/ledger.json": b"{}"}
    mock_client.create_pull_request.assert_called_once()
