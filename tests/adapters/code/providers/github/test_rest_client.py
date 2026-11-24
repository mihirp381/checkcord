from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from checkcord.adapters.code.providers.github.rest_client import GitHubRestClient


@pytest.mark.asyncio
async def test_get_file_content_success():
    client = GitHubRestClient(personal_access_token="fake_pat")

    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"print('hello')"

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        content = await client.get_file_content("owner", "repo", "main.py", "abcdef")

        assert content == b"print('hello')"
        mock_http_client.get.assert_called_once_with(
            "https://api.github.com/repos/owner/repo/contents/main.py",
            headers={
                "Authorization": "Bearer fake_pat",
                "Accept": "application/vnd.github.raw",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            params={"ref": "abcdef"},
        )


@pytest.mark.asyncio
async def test_get_file_content_404():
    client = GitHubRestClient(personal_access_token="fake_pat")

    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 404

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        content = await client.get_file_content("owner", "repo", "missing.py", "abcdef")

        assert content is None


@pytest.mark.asyncio
async def test_get_file_content_failure():
    client = GitHubRestClient(personal_access_token="fake_pat")

    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        content = await client.get_file_content("owner", "repo", "error.py", "abcdef")

        assert content is None


@pytest.mark.asyncio
async def test_create_commit_status_success():
    client = GitHubRestClient(personal_access_token="fake_pat")

    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 201

        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        success = await client.create_commit_status(
            "owner", "repo", "sha123", "success", "All checks passed", "http://target"
        )

        assert success is True
        mock_http_client.post.assert_called_once_with(
            "https://api.github.com/repos/owner/repo/statuses/sha123",
            headers={
                "Authorization": "Bearer fake_pat",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "state": "success",
                "description": "All checks passed",
                "context": "Checkcord",
                "target_url": "http://target",
            },
        )


@pytest.mark.asyncio
async def test_create_issue_comment_success():
    client = GitHubRestClient(personal_access_token="fake_pat")

    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 201

        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        success = await client.create_issue_comment("owner", "repo", 42, "This is a comment")

        assert success is True
        mock_http_client.post.assert_called_once_with(
            "https://api.github.com/repos/owner/repo/issues/42/comments",
            headers={
                "Authorization": "Bearer fake_pat",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"body": "This is a comment"},
        )


@pytest.mark.asyncio
async def test_get_ref_success() -> None:
    client = GitHubRestClient(personal_access_token="fake_pat")
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"object": {"sha": "ref_sha_123"}}
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        MockClient.return_value = mock_http_client

        sha = await client.get_ref("owner", "repo", "heads/main")
        assert sha == "ref_sha_123"


@pytest.mark.asyncio
async def test_create_ref_success() -> None:
    client = GitHubRestClient(personal_access_token="fake_pat")
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        MockClient.return_value = mock_http_client

        success = await client.create_ref("owner", "repo", "heads/new-branch", "base_sha")
        assert success is True


@pytest.mark.asyncio
async def test_update_ref_success() -> None:
    client = GitHubRestClient(personal_access_token="fake_pat")
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_http_client = AsyncMock()
        mock_http_client.patch = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        MockClient.return_value = mock_http_client

        success = await client.update_ref("owner", "repo", "heads/branch", "new_sha", force=True)
        assert success is True
        mock_http_client.patch.assert_called_once_with(
            "https://api.github.com/repos/owner/repo/git/refs/heads/branch",
            headers={
                "Authorization": "Bearer fake_pat",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"sha": "new_sha", "force": True},
        )


@pytest.mark.asyncio
async def test_update_ref_failure() -> None:
    client = GitHubRestClient(personal_access_token="fake_pat")
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 422
        mock_response.text = "Reference update failed"
        mock_http_client = AsyncMock()
        mock_http_client.patch = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        MockClient.return_value = mock_http_client

        success = await client.update_ref(
            "owner", "repo", "refs/heads/branch", "new_sha", force=True
        )
        assert success is False


@pytest.mark.asyncio
async def test_get_file_info_success() -> None:
    client = GitHubRestClient(personal_access_token="fake_pat")
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"sha": "file_sha", "size": 123}
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        MockClient.return_value = mock_http_client

        info = await client.get_file_info("owner", "repo", "path/to/file", "main")
        assert info == {"sha": "file_sha", "size": 123}


@pytest.mark.asyncio
async def test_create_or_update_file_success() -> None:
    client = GitHubRestClient(personal_access_token="fake_pat")
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"commit": {"sha": "new_commit_sha"}}
        mock_http_client = AsyncMock()
        mock_http_client.put = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        MockClient.return_value = mock_http_client

        commit_sha = await client.create_or_update_file(
            "owner", "repo", "path/to/file", "msg", b"content", "branch", "existing_sha"
        )
        assert commit_sha == "new_commit_sha"


@pytest.mark.asyncio
async def test_list_pull_requests_success() -> None:
    client = GitHubRestClient(personal_access_token="fake_pat")
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [{"number": 1}]
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        MockClient.return_value = mock_http_client

        prs = await client.list_pull_requests("owner", "repo", head="owner:branch")
        assert prs == [{"number": 1}]


@pytest.mark.asyncio
async def test_create_pull_request_success() -> None:
    client = GitHubRestClient(personal_access_token="fake_pat")
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"number": 2}
        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        MockClient.return_value = mock_http_client

        pr = await client.create_pull_request("owner", "repo", "title", "head", "base", "body")
        assert pr == {"number": 2}


@pytest.mark.asyncio
async def test_get_repository_success() -> None:
    client = GitHubRestClient(personal_access_token="fake_pat")
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"default_branch": "main"}
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        MockClient.return_value = mock_http_client

        repo = await client.get_repository("owner", "repo")
        assert repo == {"default_branch": "main"}


# ---------------------------------------------------------------------------
# Rate-limit-aware retry/backoff, and the Git Trees, Contents, Checks and
# Actions API methods.
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402

from checkcord.core.exceptions import GitHubAPIError  # noqa: E402


@pytest.mark.asyncio
async def test_request_with_retry_retries_on_429_then_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    client = GitHubRestClient(personal_access_token="fake_pat")

    rate_limited_response = MagicMock()
    rate_limited_response.status_code = 429
    rate_limited_response.headers = {}

    success_response = MagicMock()
    success_response.status_code = 200
    success_response.json.return_value = {"default_branch": "main"}

    with patch("httpx.AsyncClient") as MockClient:
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(side_effect=[rate_limited_response, success_response])
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        repo = await client.get_repository("owner", "repo")

    assert repo == {"default_branch": "main"}
    assert mock_http_client.get.call_count == 2


@pytest.mark.asyncio
async def test_request_with_retry_retries_on_secondary_rate_limit_403(monkeypatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    client = GitHubRestClient(personal_access_token="fake_pat")

    secondary_limited = MagicMock()
    secondary_limited.status_code = 403
    secondary_limited.headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "9999999999"}

    success_response = MagicMock()
    success_response.status_code = 200
    success_response.json.return_value = {"default_branch": "main"}

    with patch("httpx.AsyncClient") as MockClient:
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(side_effect=[secondary_limited, success_response])
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        repo = await client.get_repository("owner", "repo")

    assert repo == {"default_branch": "main"}
    assert mock_http_client.get.call_count == 2


@pytest.mark.asyncio
async def test_request_with_retry_does_not_retry_plain_403() -> None:
    """A 403 with no rate-limit headers is a genuine permissions error --
    must not be retried, and existing callers' falsy-return behavior on
    non-200 responses is unaffected."""
    client = GitHubRestClient(personal_access_token="fake_pat")

    forbidden_response = MagicMock()
    forbidden_response.status_code = 403
    forbidden_response.headers = {}
    forbidden_response.text = "Forbidden"

    with patch("httpx.AsyncClient") as MockClient:
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=forbidden_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        repo = await client.get_repository("owner", "repo")

    assert repo is None
    assert mock_http_client.get.call_count == 1


@pytest.mark.asyncio
async def test_request_with_retry_exhausts_retries_old_method_returns_falsy(monkeypatch) -> None:
    """get_repository still returns its falsy value after retries are
    exhausted and does not raise.
    """
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    client = GitHubRestClient(personal_access_token="fake_pat")

    always_500 = MagicMock()
    always_500.status_code = 500
    always_500.headers = {}
    always_500.text = "Internal Server Error"

    with patch("httpx.AsyncClient") as MockClient:
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=always_500)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        repo = await client.get_repository("owner", "repo")

    assert repo is None
    assert mock_http_client.get.call_count == 4  # _MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_list_repository_tree_success() -> None:
    client = GitHubRestClient(personal_access_token="fake_pat")
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "tree": [
                {"path": "src/main.py", "type": "blob"},
                {"path": "src", "type": "tree"},
            ],
            "truncated": False,
        }
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        tree = await client.list_repository_tree("owner", "repo", "main")

    assert len(tree) == 2
    assert tree[0]["path"] == "src/main.py"


@pytest.mark.asyncio
async def test_list_repository_tree_failure_raises_github_api_error() -> None:
    client = GitHubRestClient(personal_access_token="fake_pat")
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.headers = {}
        mock_response.text = "Not Found"
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        with pytest.raises(GitHubAPIError):
            await client.list_repository_tree("owner", "repo", "main")


@pytest.mark.asyncio
async def test_list_pulls_associated_with_commit_success() -> None:
    client = GitHubRestClient(personal_access_token="fake_pat")
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [{"number": 42}]
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        prs = await client.list_pulls_associated_with_commit("owner", "repo", "sha123")

    assert prs == [{"number": 42}]


@pytest.mark.asyncio
async def test_list_pulls_associated_with_commit_no_pr_returns_empty() -> None:
    """A direct push to the default branch has no associated PR: an empty
    result, not an error.
    """
    client = GitHubRestClient(personal_access_token="fake_pat")
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        prs = await client.list_pulls_associated_with_commit("owner", "repo", "sha123")

    assert prs == []


@pytest.mark.asyncio
async def test_list_check_runs_for_ref_success() -> None:
    client = GitHubRestClient(personal_access_token="fake_pat")
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"check_runs": [{"id": 1, "conclusion": "failure"}]}
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        runs = await client.list_check_runs_for_ref("owner", "repo", "sha123")

    assert len(runs) == 1
    assert runs[0]["conclusion"] == "failure"


@pytest.mark.asyncio
async def test_get_check_run_annotations_success() -> None:
    client = GitHubRestClient(personal_access_token="fake_pat")
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [{"path": "x.py", "message": "boom"}]
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        annotations = await client.get_check_run_annotations("owner", "repo", 1)

    assert annotations == [{"path": "x.py", "message": "boom"}]


@pytest.mark.asyncio
async def test_list_workflow_jobs_for_run_success() -> None:
    client = GitHubRestClient(personal_access_token="fake_pat")
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"jobs": [{"id": 1, "conclusion": "failure"}]}
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        jobs = await client.list_workflow_jobs_for_run("owner", "repo", 999)

    assert len(jobs) == 1


@pytest.mark.asyncio
async def test_download_job_log_success() -> None:
    client = GitHubRestClient(personal_access_token="fake_pat")
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "log line 1\nlog line 2\n"
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        log = await client.download_job_log("owner", "repo", 111)

    assert log is not None
    assert "log line 1" in log


@pytest.mark.asyncio
async def test_download_job_log_expired_returns_none() -> None:
    client = GitHubRestClient(personal_access_token="fake_pat")
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 410
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        log = await client.download_job_log("owner", "repo", 111)

    assert log is None


@pytest.mark.asyncio
async def test_download_job_log_refuses_oversized_log() -> None:
    """A log advertising a size beyond the hard ceiling is refused before its
    body is read.
    """
    client = GitHubRestClient(personal_access_token="fake_pat")
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Length": str(21 * 1024 * 1024)}
        mock_response.text = "should never be read"
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        log = await client.download_job_log("owner", "repo", 111)

    assert log is None
