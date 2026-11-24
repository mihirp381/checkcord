import asyncio
import base64
import time
from typing import Any

import httpx
import structlog

from checkcord.core.config import get_settings
from checkcord.core.exceptions import GitHubAPIError

logger = structlog.get_logger(__name__).bind(component="github_client")

# Statuses worth retrying: secondary rate limit (403 w/ rate-limit headers),
# primary rate limit (429), and transient server-side failures. A 403 without
# rate-limit headers is a genuine permissions error, not retried (see
# _is_retryable_response).
_RETRYABLE_STATUS_CODES = {403, 429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4
_MAX_BACKOFF_SECONDS = 30.0
_MAX_COLLABORATOR_PAGES = 50
_MAX_FILE_PAGES = 30
_MAX_COMMENT_PAGES = 50


def _is_retryable_response(response: httpx.Response) -> bool:
    if response.status_code in (429, 500, 502, 503, 504):
        return True
    if response.status_code == 403:
        # GitHub signals both primary and secondary rate limiting via these
        # headers on a 403; a 403 without either is a real permissions error.
        return response.headers.get("X-RateLimit-Remaining") == "0" or (
            "Retry-After" in response.headers
        )
    return False


def _backoff_delay(attempt: int, response: httpx.Response | None = None) -> float:
    """Honor GitHub's own Retry-After / X-RateLimit-Reset hints when present,
    else fall back to exponential backoff -- same style as
    bus/redis_streams.py's circuit-breaker backoff (capped, doubling).
    """
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), _MAX_BACKOFF_SECONDS)
            except ValueError:
                pass
        reset = response.headers.get("X-RateLimit-Reset")
        remaining = response.headers.get("X-RateLimit-Remaining")
        if reset and remaining == "0":
            try:
                wait = float(reset) - time.time()
                if wait > 0:
                    return min(wait, _MAX_BACKOFF_SECONDS)
            except ValueError:
                pass
    return min(2.0**attempt, _MAX_BACKOFF_SECONDS)


class GitHubRestClient:
    """GitHub REST client."""

    def __init__(self, personal_access_token: str) -> None:
        self.pat = personal_access_token
        self.base_url = get_settings().github_api_url
        self.headers = {
            "Authorization": f"Bearer {self.pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _request_with_retry(
        self, http_method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        """Issue one HTTP request with rate-limit-aware retry/backoff.

        Forwards ``**kwargs`` verbatim to the underlying ``httpx.AsyncClient``
        verb method (``get``/``post``/``patch``/``put``) so every existing call
        site's exact call shape (and therefore its existing tests' mock
        assertions) is preserved unchanged on the common one-attempt-succeeds
        path -- this helper only changes behavior when a response is actually
        retryable (see ``_is_retryable_response``) or a transport error occurs.

        Returns the final ``httpx.Response`` (whatever its status code) once
        retries are exhausted or a non-retryable status is hit; callers handle
        the status code. Raises ``GitHubAPIError`` only for transport-level
        failures (timeout/connection error) that persist across every attempt.
        """
        last_exc: Exception | None = None
        async with httpx.AsyncClient(timeout=10.0) as client:
            call = getattr(client, http_method)
            for attempt in range(_MAX_ATTEMPTS):
                try:
                    response: httpx.Response = await call(url, **kwargs)
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    last_exc = e
                    if attempt < _MAX_ATTEMPTS - 1:
                        delay = _backoff_delay(attempt)
                        logger.warning(
                            "github_client.transport_error_retrying",
                            url=url,
                            attempt=attempt + 1,
                            delay=delay,
                            error=str(e),
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise GitHubAPIError(
                        f"GitHub API request to {url} failed after {_MAX_ATTEMPTS} attempts: {e}"
                    ) from e

                if not _is_retryable_response(response) or attempt == _MAX_ATTEMPTS - 1:
                    return response

                delay = _backoff_delay(attempt, response)
                logger.warning(
                    "github_client.rate_limited_retrying",
                    url=url,
                    status_code=response.status_code,
                    attempt=attempt + 1,
                    delay=delay,
                )
                await asyncio.sleep(delay)

        # Unreachable in practice (the loop always returns or raises), but
        # keeps mypy satisfied that every path yields a value.
        if last_exc is not None:
            raise GitHubAPIError(f"GitHub API request to {url} failed: {last_exc}") from last_exc
        raise GitHubAPIError(f"GitHub API request to {url} failed with no response")

    async def _pages(self, url: str, max_pages: int) -> list[Any]:
        """Every item of a paged list, following ``Link: rel="next"``; raises on any failure."""
        items: list[Any] = []
        next_url: str | None = url
        params: dict[str, Any] | None = {"per_page": 100}
        for _ in range(max_pages):
            if next_url is None:
                return items
            response = await self._request_with_retry(
                "get", next_url, headers=self.headers, params=params
            )
            if response.status_code != 200:
                raise GitHubAPIError(f"GET {url} returned {response.status_code}")
            items.extend(response.json())
            next_url = response.links.get("next", {}).get("url")
            params = None
        if next_url is not None:
            raise GitHubAPIError(f"GET {url} has more than {max_pages} pages")
        return items

    async def list_pull_request_files(
        self, owner: str, repo: str, pull_number: int
    ) -> list[dict[str, Any]]:
        """Every changed file with its ``filename``, ``status`` and ``patch`` (absent for
        binary or very large files). GitHub lists at most 3,000."""
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls/{pull_number}/files"
        return await self._pages(url, _MAX_FILE_PAGES)

    async def get_pull_request_files(
        self, owner: str, repo: str, pull_number: int
    ) -> tuple[list[str], int, int]:
        """Names of the files changed and total additions/deletions; empty on failure."""
        try:
            files_data = await self.list_pull_request_files(owner, repo, pull_number)
        except Exception as e:
            logger.error(
                "error_fetching_pr_files",
                error=str(e),
                owner=owner,
                repo=repo,
                pull_number=pull_number,
            )
            return [], 0, 0
        files_changed = [f["filename"] for f in files_data]
        additions = sum(f.get("additions", 0) for f in files_data)
        deletions = sum(f.get("deletions", 0) for f in files_data)
        return files_changed, additions, deletions

    async def get_commit_details(
        self, owner: str, repo: str, commit_sha: str
    ) -> tuple[list[str], int, int]:
        """Fetch files changed and total additions/deletions for a specific commit.

        Capped at 100 files.
        """
        url = f"{self.base_url}/repos/{owner}/{repo}/commits/{commit_sha}"

        try:
            response = await self._request_with_retry("get", url, headers=self.headers)
            if response.status_code != 200:
                logger.error(
                    "failed_to_fetch_commit_details",
                    status_code=response.status_code,
                    owner=owner,
                    repo=repo,
                    commit_sha=commit_sha,
                    body=response.text,
                )
                return [], 0, 0

            commit_data = response.json()
            stats = commit_data.get("stats", {})
            additions = stats.get("additions", 0)
            deletions = stats.get("deletions", 0)

            files_data = commit_data.get("files", [])
            files_changed = [f["filename"] for f in files_data[:100]]

            return files_changed, additions, deletions
        except Exception as e:
            logger.error(
                "error_fetching_commit_details",
                error=str(e),
                owner=owner,
                repo=repo,
                commit_sha=commit_sha,
            )
            return [], 0, 0

    async def get_collaborators(self, owner: str, repo: str) -> list[str]:
        """Fetch every collaborator login for a repository; [] if any page fails."""
        url = f"{self.base_url}/repos/{owner}/{repo}/collaborators"
        per_page = 100
        logins: list[str] = []

        try:
            for page in range(1, _MAX_COLLABORATOR_PAGES + 1):
                params = {"per_page": per_page, "page": page}
                response = await self._request_with_retry(
                    "get", url, headers=self.headers, params=params
                )
                if response.status_code != 200:
                    logger.error(
                        "failed_to_fetch_collaborators",
                        status_code=response.status_code,
                        owner=owner,
                        repo=repo,
                        body=response.text,
                    )
                    return []

                collaborators_data = response.json()
                logins.extend(c["login"] for c in collaborators_data if "login" in c)
                if len(collaborators_data) < per_page:
                    return logins
            # A partial list must not pass for the whole audience.
            logger.error("collaborator_list_truncated", owner=owner, repo=repo)
            return []
        except Exception as e:
            logger.error(
                "error_fetching_collaborators",
                error=str(e),
                owner=owner,
                repo=repo,
            )
            return []

    async def get_file_content(self, owner: str, repo: str, path: str, ref: str) -> bytes | None:
        """Fetch raw content of a file at a specific ref/commit_sha."""
        url = f"{self.base_url}/repos/{owner}/{repo}/contents/{path}"
        params = {"ref": ref}
        headers = dict(self.headers)
        headers["Accept"] = "application/vnd.github.raw"

        try:
            response = await self._request_with_retry("get", url, headers=headers, params=params)
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                logger.error(
                    "failed_to_fetch_file_content",
                    status_code=response.status_code,
                    owner=owner,
                    repo=repo,
                    path=path,
                    ref=ref,
                    body=response.text,
                )
                return None
            return bytes(response.content)
        except Exception as e:
            logger.error(
                "error_fetching_file_content",
                error=str(e),
                owner=owner,
                repo=repo,
                path=path,
                ref=ref,
            )
            return None

    async def create_commit_status(
        self,
        owner: str,
        repo: str,
        sha: str,
        state: str,
        description: str,
        target_url: str = "",
        context: str = "Checkcord",
    ) -> bool:
        """Create a commit status check on a specific SHA."""
        url = f"{self.base_url}/repos/{owner}/{repo}/statuses/{sha}"
        payload = {
            "state": state,
            "description": description[:140],
            "context": context,
        }
        if target_url:
            payload["target_url"] = target_url

        try:
            response = await self._request_with_retry(
                "post", url, headers=self.headers, json=payload
            )
            if response.status_code != 201:
                logger.error(
                    "failed_to_create_commit_status",
                    status_code=response.status_code,
                    owner=owner,
                    repo=repo,
                    sha=sha,
                    body=response.text,
                )
                return False
            return True
        except Exception as e:
            logger.error(
                "error_creating_commit_status",
                error=str(e),
                owner=owner,
                repo=repo,
                sha=sha,
            )
            return False

    async def create_issue_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        body: str,
    ) -> bool:
        """Post a comment on a pull request (which is an issue under the hood)."""
        url = f"{self.base_url}/repos/{owner}/{repo}/issues/{issue_number}/comments"
        payload = {"body": body}

        try:
            response = await self._request_with_retry(
                "post", url, headers=self.headers, json=payload
            )
            if response.status_code != 201:
                logger.error(
                    "failed_to_create_issue_comment",
                    status_code=response.status_code,
                    owner=owner,
                    repo=repo,
                    issue_number=issue_number,
                    body=response.text,
                )
                return False
            return True
        except Exception as e:
            logger.error(
                "error_creating_issue_comment",
                error=str(e),
                owner=owner,
                repo=repo,
                issue_number=issue_number,
            )
            return False

    async def list_issue_comments(
        self, owner: str, repo: str, issue_number: int
    ) -> list[dict[str, Any]]:
        """Every comment on an issue or pull request; raises on any failure."""
        url = f"{self.base_url}/repos/{owner}/{repo}/issues/{issue_number}/comments"
        return await self._pages(url, _MAX_COMMENT_PAGES)

    async def update_issue_comment(self, owner: str, repo: str, comment_id: int, body: str) -> bool:
        url = f"{self.base_url}/repos/{owner}/{repo}/issues/comments/{comment_id}"
        try:
            response = await self._request_with_retry(
                "patch", url, headers=self.headers, json={"body": body}
            )
        except Exception as e:
            logger.error("error_updating_issue_comment", error=str(e), comment_id=comment_id)
            return False
        if response.status_code != 200:
            logger.error(
                "failed_to_update_issue_comment",
                status_code=response.status_code,
                comment_id=comment_id,
                body=response.text,
            )
            return False
        return True

    async def get_ref(self, owner: str, repo: str, ref: str) -> str | None:
        """Fetch SHA of a Git reference (e.g. heads/main or heads/checkcord/update-context)."""
        url = f"{self.base_url}/repos/{owner}/{repo}/git/ref/{ref}"
        try:
            response = await self._request_with_retry("get", url, headers=self.headers)
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                logger.error(
                    "failed_to_get_ref",
                    status_code=response.status_code,
                    owner=owner,
                    repo=repo,
                    ref=ref,
                    body=response.text,
                )
                return None
            data = response.json()
            return str(data["object"]["sha"])
        except Exception as e:
            logger.error(
                "error_getting_ref",
                error=str(e),
                owner=owner,
                repo=repo,
                ref=ref,
            )
            return None

    async def create_ref(self, owner: str, repo: str, ref: str, sha: str) -> bool:
        """Create a new Git reference (e.g. refs/heads/checkcord/update-context)
        pointing to a SHA.
        """
        url = f"{self.base_url}/repos/{owner}/{repo}/git/refs"
        payload = {
            "ref": ref if ref.startswith("refs/") else f"refs/{ref}",
            "sha": sha,
        }
        try:
            response = await self._request_with_retry(
                "post", url, headers=self.headers, json=payload
            )
            if response.status_code != 201:
                logger.error(
                    "failed_to_create_ref",
                    status_code=response.status_code,
                    owner=owner,
                    repo=repo,
                    ref=ref,
                    sha=sha,
                    body=response.text,
                )
                return False
            return True
        except Exception as e:
            logger.error(
                "error_creating_ref",
                error=str(e),
                owner=owner,
                repo=repo,
                ref=ref,
                sha=sha,
            )
            return False

    async def update_ref(
        self, owner: str, repo: str, ref: str, sha: str, force: bool = True
    ) -> bool:
        """Update an existing Git reference (e.g. heads/checkcord/update-context)
        to point to a new SHA.
        """
        clean_ref = ref
        if clean_ref.startswith("refs/"):
            clean_ref = clean_ref[len("refs/") :]
        url = f"{self.base_url}/repos/{owner}/{repo}/git/refs/{clean_ref}"
        payload = {
            "sha": sha,
            "force": force,
        }
        try:
            response = await self._request_with_retry(
                "patch", url, headers=self.headers, json=payload
            )
            if response.status_code != 200:
                logger.error(
                    "failed_to_update_ref",
                    status_code=response.status_code,
                    owner=owner,
                    repo=repo,
                    ref=ref,
                    sha=sha,
                    force=force,
                    body=response.text,
                )
                return False
            return True
        except Exception as e:
            logger.error(
                "error_updating_ref",
                error=str(e),
                owner=owner,
                repo=repo,
                ref=ref,
                sha=sha,
                force=force,
            )
            return False

    async def get_file_info(self, owner: str, repo: str, path: str, ref: str) -> dict | None:
        """Fetch metadata for a file at a specific ref (contains the file's current SHA)."""
        url = f"{self.base_url}/repos/{owner}/{repo}/contents/{path}"
        params = {"ref": ref}
        try:
            response = await self._request_with_retry(
                "get", url, headers=self.headers, params=params
            )
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                logger.error(
                    "failed_to_get_file_info",
                    status_code=response.status_code,
                    owner=owner,
                    repo=repo,
                    path=path,
                    ref=ref,
                    body=response.text,
                )
                return None
            return dict(response.json())
        except Exception as e:
            logger.error(
                "error_getting_file_info",
                error=str(e),
                owner=owner,
                repo=repo,
                path=path,
                ref=ref,
            )
            return None

    async def list_repository_tree(
        self, owner: str, repo: str, tree_sha_or_ref: str, recursive: bool = True
    ) -> list[dict]:
        """List every entry (blob/tree) in a repository via the Git Trees API.

        ``tree_sha_or_ref`` may be a branch name, a tag or a tree SHA. GitHub caps
        one recursive response at about 100,000 entries / 7MB and sets
        ``truncated: true`` beyond that; this is logged as a warning and the
        partial tree is returned.

        Raises ``GitHubAPIError`` on any non-2xx response or transport failure;
        callers must not treat a failed listing as an empty repo.
        """
        url = f"{self.base_url}/repos/{owner}/{repo}/git/trees/{tree_sha_or_ref}"
        params: dict[str, str | int] = {"recursive": "1"} if recursive else {}
        try:
            response = await self._request_with_retry(
                "get", url, headers=self.headers, params=params
            )
        except GitHubAPIError:
            raise
        except Exception as e:
            raise GitHubAPIError(
                f"Error listing repository tree for {owner}/{repo}@{tree_sha_or_ref}: {e}"
            ) from e

        if response.status_code != 200:
            raise GitHubAPIError(
                f"Failed to list repository tree for {owner}/{repo}@{tree_sha_or_ref}: "
                f"HTTP {response.status_code}: {response.text}",
                status_code=response.status_code,
            )

        data = response.json()
        if data.get("truncated"):
            logger.warning(
                "github_client.tree_listing_truncated",
                owner=owner,
                repo=repo,
                ref=tree_sha_or_ref,
                entry_count=len(data.get("tree", [])),
            )
        return list(data.get("tree", []))

    async def create_or_update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        message: str,
        content: bytes,
        branch: str,
        sha: str | None = None,
    ) -> str | None:
        """Create or update a file on a specific branch. Returns the new commit SHA or None."""
        url = f"{self.base_url}/repos/{owner}/{repo}/contents/{path}"
        encoded_content = base64.b64encode(content).decode("utf-8")
        payload = {
            "message": message,
            "content": encoded_content,
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha

        try:
            response = await self._request_with_retry(
                "put", url, headers=self.headers, json=payload
            )
            if response.status_code not in (200, 201):
                logger.error(
                    "failed_to_create_or_update_file",
                    status_code=response.status_code,
                    owner=owner,
                    repo=repo,
                    path=path,
                    branch=branch,
                    body=response.text,
                )
                return None
            data = response.json()
            return str(data["commit"]["sha"])
        except Exception as e:
            logger.error(
                "error_creating_or_updating_file",
                error=str(e),
                owner=owner,
                repo=repo,
                path=path,
                branch=branch,
            )
            return None

    async def list_pull_requests(
        self, owner: str, repo: str, state: str = "open", head: str | None = None
    ) -> list[dict]:
        """List pull requests for a repository."""
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls"
        params: dict[str, str | int] = {"state": state, "per_page": 100}
        if head:
            params["head"] = head

        try:
            response = await self._request_with_retry(
                "get", url, headers=self.headers, params=params
            )
            if response.status_code != 200:
                logger.error(
                    "failed_to_list_pull_requests",
                    status_code=response.status_code,
                    owner=owner,
                    repo=repo,
                    body=response.text,
                )
                return []
            return list(response.json())
        except Exception as e:
            logger.error(
                "error_listing_pull_requests",
                error=str(e),
                owner=owner,
                repo=repo,
            )
            return []

    async def list_pulls_associated_with_commit(
        self, owner: str, repo: str, commit_sha: str
    ) -> list[dict]:
        """List pull requests associated with a commit SHA.

        Used to resolve which PR (if any) caused a given CI run's commit --
        a direct push to the default branch or a scheduled/manual workflow
        run has no associated PR, which is a legitimate empty result, not an
        error (see M13's graceful-degrade requirement for failure-to-AST
        linking).
        """
        url = f"{self.base_url}/repos/{owner}/{repo}/commits/{commit_sha}/pulls"
        try:
            response = await self._request_with_retry("get", url, headers=self.headers)
        except GitHubAPIError:
            raise
        except Exception as e:
            raise GitHubAPIError(
                f"Error listing PRs associated with commit {commit_sha} in {owner}/{repo}: {e}"
            ) from e

        if response.status_code == 404:
            return []
        if response.status_code != 200:
            raise GitHubAPIError(
                f"Failed to list PRs associated with commit {commit_sha} in {owner}/{repo}: "
                f"HTTP {response.status_code}",
                status_code=response.status_code,
            )
        return list(response.json())

    async def list_check_runs_for_ref(self, owner: str, repo: str, ref: str) -> list[dict]:
        """List check runs (Checks API) for a given ref (commit SHA).

        Preferred source of structured failure content (path, line, message).
        """
        url = f"{self.base_url}/repos/{owner}/{repo}/commits/{ref}/check-runs"
        try:
            response = await self._request_with_retry("get", url, headers=self.headers)
        except GitHubAPIError:
            raise
        except Exception as e:
            raise GitHubAPIError(f"Error listing check runs for {owner}/{repo}@{ref}: {e}") from e

        if response.status_code != 200:
            raise GitHubAPIError(
                f"Failed to list check runs for {owner}/{repo}@{ref}: HTTP {response.status_code}",
                status_code=response.status_code,
            )
        data = response.json()
        return list(data.get("check_runs", []))

    async def get_check_run_annotations(
        self, owner: str, repo: str, check_run_id: int
    ) -> list[dict]:
        """Fetch annotations (path/line/message) for a single check run."""
        url = f"{self.base_url}/repos/{owner}/{repo}/check-runs/{check_run_id}/annotations"
        try:
            response = await self._request_with_retry("get", url, headers=self.headers)
        except GitHubAPIError:
            raise
        except Exception as e:
            raise GitHubAPIError(
                f"Error fetching annotations for check run {check_run_id} in {owner}/{repo}: {e}"
            ) from e

        if response.status_code == 404:
            return []
        if response.status_code != 200:
            raise GitHubAPIError(
                f"Failed to fetch annotations for check run {check_run_id} in {owner}/{repo}: "
                f"HTTP {response.status_code}",
                status_code=response.status_code,
            )
        return list(response.json())

    async def list_workflow_jobs_for_run(self, owner: str, repo: str, run_id: int) -> list[dict]:
        """List jobs for a workflow run, including each job's per-step conclusions."""
        url = f"{self.base_url}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
        try:
            response = await self._request_with_retry("get", url, headers=self.headers)
        except GitHubAPIError:
            raise
        except Exception as e:
            raise GitHubAPIError(
                f"Error listing jobs for run {run_id} in {owner}/{repo}: {e}"
            ) from e

        if response.status_code != 200:
            raise GitHubAPIError(
                f"Failed to list jobs for run {run_id} in {owner}/{repo}: "
                f"HTTP {response.status_code}",
                status_code=response.status_code,
            )
        data = response.json()
        return list(data.get("jobs", []))

    # Hard ceiling on one job-log download, independent of the caller's
    # max_log_lines. GitHub's redirect target has no reliable "last N bytes"
    # range request, so an oversized log is refused (logged, treated as
    # unavailable) instead of being pulled into memory.
    _MAX_JOB_LOG_DOWNLOAD_BYTES = 20 * 1024 * 1024

    async def download_job_log(self, owner: str, repo: str, job_id: int) -> str | None:
        """Download the plain-text log for a single job.

        GitHub responds with a redirect to a time-limited download URL;
        httpx follows redirects by default only when configured to, so this
        method explicitly enables it. Returns ``None`` (not an exception) if
        the log has expired/is unavailable (404/410) -- GitHub only retains
        Actions logs for a limited window, and an expired log is a normal,
        expected condition (see edge cases), not a failure the caller should
        treat as a broken request. Also returns ``None`` (with a logged
        warning) rather than downloading if the log is larger than
        ``_MAX_JOB_LOG_DOWNLOAD_BYTES``.
        """
        url = f"{self.base_url}/repos/{owner}/{repo}/actions/jobs/{job_id}/logs"
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                response = await client.get(url, headers=self.headers)
        except Exception as e:
            raise GitHubAPIError(
                f"Error downloading job log for job {job_id} in {owner}/{repo}: {e}"
            ) from e

        if response.status_code in (404, 410):
            return None
        if response.status_code != 200:
            raise GitHubAPIError(
                f"Failed to download job log for job {job_id} in {owner}/{repo}: "
                f"HTTP {response.status_code}",
                status_code=response.status_code,
            )

        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > self._MAX_JOB_LOG_DOWNLOAD_BYTES:
            logger.warning(
                "github_client.job_log_too_large_skipping",
                job_id=job_id,
                owner=owner,
                repo=repo,
                content_length=content_length,
            )
            return None

        return response.text

    async def create_pull_request(
        self, owner: str, repo: str, title: str, head: str, base: str, body: str
    ) -> dict | None:
        """Create a new Pull Request. Returns the response JSON or None."""
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls"
        payload = {
            "title": title,
            "head": head,
            "base": base,
            "body": body,
        }
        try:
            response = await self._request_with_retry(
                "post", url, headers=self.headers, json=payload
            )
            if response.status_code != 201:
                logger.error(
                    "failed_to_create_pull_request",
                    status_code=response.status_code,
                    owner=owner,
                    repo=repo,
                    head=head,
                    base=base,
                    body=response.text,
                )
                return None
            return dict(response.json())
        except Exception as e:
            logger.error(
                "error_creating_pull_request",
                error=str(e),
                owner=owner,
                repo=repo,
                head=head,
                base=base,
            )
            return None

    async def get_user(self, login: str) -> dict | None:
        """A user's public profile; None when it cannot be read."""
        url = f"{self.base_url}/users/{login}"
        try:
            response = await self._request_with_retry("get", url, headers=self.headers)
        except Exception as e:
            logger.warning("error_getting_user", error=str(e), login=login)
            return None
        if response.status_code != 200:
            logger.warning("failed_to_get_user", status_code=response.status_code, login=login)
            return None
        return dict(response.json())

    async def get_pull_request(self, owner: str, repo: str, number: int) -> dict | None:
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls/{number}"
        try:
            response = await self._request_with_retry("get", url, headers=self.headers)
        except Exception as e:
            logger.warning("error_getting_pull_request", error=str(e), repo=f"{owner}/{repo}")
            return None
        if response.status_code != 200:
            logger.warning(
                "failed_to_get_pull_request", status_code=response.status_code, number=number
            )
            return None
        return dict(response.json())

    async def get_repository(self, owner: str, repo: str) -> dict | None:
        """Fetch repository details (e.g. to get the default branch)."""
        url = f"{self.base_url}/repos/{owner}/{repo}"
        try:
            response = await self._request_with_retry("get", url, headers=self.headers)
            if response.status_code != 200:
                logger.error(
                    "failed_to_get_repository",
                    status_code=response.status_code,
                    owner=owner,
                    repo=repo,
                    body=response.text,
                )
                return None
            return dict(response.json())
        except Exception as e:
            logger.error(
                "error_getting_repository",
                error=str(e),
                owner=owner,
                repo=repo,
            )
            return None

    async def has_recent_commits(self, owner: str, repo: str, since_days: int = 30) -> bool:
        """Check if the repository has commits in the last since_days days."""
        from datetime import UTC, datetime, timedelta

        since_time = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()
        url = f"{self.base_url}/repos/{owner}/{repo}/commits"
        params: dict[str, str | int] = {"since": since_time, "per_page": 1}
        try:
            response = await self._request_with_retry(
                "get", url, headers=self.headers, params=params
            )
            if response.status_code != 200:
                logger.error(
                    "failed_to_check_recent_commits",
                    status_code=response.status_code,
                    owner=owner,
                    repo=repo,
                )
                return False
            commits = response.json()
            return len(commits) > 0
        except Exception as e:
            logger.error(
                "error_checking_recent_commits",
                error=str(e),
                owner=owner,
                repo=repo,
            )
            return False
