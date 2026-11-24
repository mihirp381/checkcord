from unittest.mock import AsyncMock, patch

import pytest

from checkcord.adapters.cicd.providers.github.provider import GitHubCICDProvider
from checkcord.core.exceptions import GitHubAPIError


@patch("checkcord.adapters.cicd.providers.github.provider.GitHubRestClient")
@pytest.mark.asyncio
async def test_fetch_failure_content_prefers_annotations(MockClient) -> None:
    client_mock = MockClient.return_value
    client_mock.list_check_runs_for_ref = AsyncMock(
        return_value=[{"id": 1, "conclusion": "failure"}, {"id": 2, "conclusion": "success"}]
    )
    client_mock.get_check_run_annotations = AsyncMock(
        return_value=[
            {
                "path": "tests/test_x.py",
                "start_line": 10,
                "end_line": 10,
                "message": "AssertionError",
                "annotation_level": "failure",
            }
        ]
    )
    # These must NOT be called when annotations are present.
    client_mock.list_workflow_jobs_for_run = AsyncMock()
    client_mock.download_job_log = AsyncMock()

    provider = GitHubCICDProvider()
    result = await provider.fetch_failure_content("owner", "repo", "sha123", 999, "pat")

    assert result.source == "annotations"
    assert len(result.annotations) == 1
    assert result.annotations[0].path == "tests/test_x.py"
    client_mock.list_workflow_jobs_for_run.assert_not_called()
    client_mock.download_job_log.assert_not_called()
    # Only the failed check run's annotations are fetched, not the successful one.
    client_mock.get_check_run_annotations.assert_called_once_with("owner", "repo", 1)


@patch("checkcord.adapters.cicd.providers.github.provider.GitHubRestClient")
@pytest.mark.asyncio
async def test_fetch_failure_content_falls_back_to_job_log(MockClient) -> None:
    client_mock = MockClient.return_value
    client_mock.list_check_runs_for_ref = AsyncMock(return_value=[])
    client_mock.list_workflow_jobs_for_run = AsyncMock(
        return_value=[
            {"id": 111, "name": "test", "conclusion": "failure"},
            {"id": 112, "name": "lint", "conclusion": "success"},
        ]
    )
    client_mock.download_job_log = AsyncMock(return_value="line1\nline2\nline3\n")

    provider = GitHubCICDProvider()
    result = await provider.fetch_failure_content("owner", "repo", "sha123", 999, "pat")

    assert result.source == "job_log"
    assert result.log_excerpt is not None
    assert "line1" in result.log_excerpt
    # Only the failed job's log is downloaded, not the successful one.
    client_mock.download_job_log.assert_called_once_with("owner", "repo", 111)


@patch("checkcord.adapters.cicd.providers.github.provider.GitHubRestClient")
@pytest.mark.asyncio
async def test_fetch_failure_content_job_log_respects_max_lines(MockClient) -> None:
    client_mock = MockClient.return_value
    client_mock.list_check_runs_for_ref = AsyncMock(return_value=[])
    client_mock.list_workflow_jobs_for_run = AsyncMock(
        return_value=[{"id": 111, "name": "test", "conclusion": "failure"}]
    )
    big_log = "\n".join(f"line-{i}" for i in range(1000))
    client_mock.download_job_log = AsyncMock(return_value=big_log)

    provider = GitHubCICDProvider()
    result = await provider.fetch_failure_content(
        "owner", "repo", "sha123", 999, "pat", max_log_lines=50
    )

    assert result.source == "job_log"
    assert result.log_excerpt is not None
    kept_lines = [line for line in result.log_excerpt.split("\n") if line.startswith("line-")]
    assert len(kept_lines) == 50
    # Must keep the *last* N lines (failure signal is near the end), not the first.
    assert "line-999" in result.log_excerpt
    assert "line-0" not in kept_lines


@patch("checkcord.adapters.cicd.providers.github.provider.GitHubRestClient")
@pytest.mark.asyncio
async def test_fetch_failure_content_unavailable_when_nothing_found(MockClient) -> None:
    client_mock = MockClient.return_value
    client_mock.list_check_runs_for_ref = AsyncMock(return_value=[])
    client_mock.list_workflow_jobs_for_run = AsyncMock(return_value=[])

    provider = GitHubCICDProvider()
    result = await provider.fetch_failure_content("owner", "repo", "sha123", 999, "pat")

    assert result.source == "unavailable"
    assert result.annotations == []
    assert result.log_excerpt is None


@patch("checkcord.adapters.cicd.providers.github.provider.GitHubRestClient")
@pytest.mark.asyncio
async def test_fetch_failure_content_degrades_gracefully_on_api_error(MockClient) -> None:
    """A GitHubAPIError from any sub-call does not propagate: the next fallback
    is tried, or 'unavailable' is returned if all sources fail.
    """
    client_mock = MockClient.return_value
    client_mock.list_check_runs_for_ref = AsyncMock(side_effect=GitHubAPIError("boom"))
    client_mock.list_workflow_jobs_for_run = AsyncMock(side_effect=GitHubAPIError("boom"))

    provider = GitHubCICDProvider()
    result = await provider.fetch_failure_content("owner", "repo", "sha123", 999, "pat")

    assert result.source == "unavailable"


@patch("checkcord.adapters.cicd.providers.github.provider.GitHubRestClient")
@pytest.mark.asyncio
async def test_fetch_failure_content_job_log_download_failure_is_skipped(MockClient) -> None:
    """A GitHubAPIError downloading one job's log must not crash the whole
    call -- degrades to 'unavailable' rather than raising."""
    client_mock = MockClient.return_value
    client_mock.list_check_runs_for_ref = AsyncMock(return_value=[])
    client_mock.list_workflow_jobs_for_run = AsyncMock(
        return_value=[{"id": 111, "name": "test", "conclusion": "failure"}]
    )
    client_mock.download_job_log = AsyncMock(side_effect=GitHubAPIError("expired"))

    provider = GitHubCICDProvider()
    result = await provider.fetch_failure_content("owner", "repo", "sha123", 999, "pat")

    assert result.source == "unavailable"


@patch("checkcord.adapters.cicd.providers.github.provider.GitHubRestClient")
@pytest.mark.asyncio
async def test_fetch_workflow_definition_missing_file_returns_none(MockClient) -> None:
    client_mock = MockClient.return_value
    client_mock.get_file_content = AsyncMock(return_value=None)

    provider = GitHubCICDProvider()
    result = await provider.fetch_workflow_definition(
        "owner", "repo", ".github/workflows/ci.yml", "main", "pat"
    )
    assert result is None


@patch("checkcord.adapters.cicd.providers.github.provider.GitHubRestClient")
@pytest.mark.asyncio
async def test_fetch_workflow_definition_parses_content(MockClient) -> None:
    client_mock = MockClient.return_value
    client_mock.get_file_content = AsyncMock(
        return_value=b"name: CI\njobs:\n  test:\n    steps:\n      - run: pytest\n"
    )

    provider = GitHubCICDProvider()
    result = await provider.fetch_workflow_definition(
        "owner", "repo", ".github/workflows/ci.yml", "main", "pat"
    )
    assert result is not None
    assert result.name == "CI"
    assert len(result.jobs) == 1
