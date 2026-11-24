import asyncio
import json
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pr_check_fakes import FakeJudge, FakePullRequest

from checkcord.models.common import AccessControl
from checkcord.models.enums import SourcePlatform
from checkcord.models.pr_checks import PRContextCheckedPayload
from checkcord.services.repo_audience import RepoAudience
from checkcord.workers import pr_status_reporter
from checkcord.workers.pr_status_reporter import (
    COMMENT_MARKER,
    RETRY_ZSET,
    Candidate,
    PRStatusWorker,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def token_lookup(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    lookup = AsyncMock(return_value="mock-pat")
    monkeypatch.setattr("checkcord.workers.pr_status_reporter.github_token_for", lookup)
    return lookup


PUBLIC = {"is_public": True}
MEMBER = str(uuid4())


def _wire_payload(payload: PRContextCheckedPayload) -> dict[str, str]:
    """Build the single-field wire format PRWorker publishes."""
    return {"payload": payload.model_dump_json()}


def _record(
    summary: str, severity: str = "hard_block", access: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "summary": summary,
        "summary_redacted": None,
        "severity": severity,
        "access_control": PUBLIC if access is None else access,
    }


@dataclass
class Rig:
    github: FakePullRequest
    redis: AsyncMock
    worker: PRStatusWorker
    judge: FakeJudge
    candidates: list[Candidate]

    async def push(self, sha: str = "abcdef123456", **fields: Any) -> None:
        self.worker._candidates = AsyncMock(  # type: ignore[method-assign]
            return_value=([Candidate(c.record, list(c.symbols)) for c in self.candidates], 0)
        )
        payload = PRContextCheckedPayload(
            repository_full_name="owner/repo",
            pull_request_number=42,
            head_sha=sha,
            tenant_id=uuid4(),
            diff="+    return float(total)",
            **fields,
        )
        await self.worker._process_message("t", "g", "1-0", _wire_payload(payload))

    def retry(self) -> PRContextCheckedPayload:
        _, mapping = self.redis.zadd.call_args.args
        return PRContextCheckedPayload.model_validate_json(next(iter(mapping)))


def _due(item: bytes) -> Any:
    return lambda key, *_: [item] if key == "ci.pr.fallback_zset" else []


@asynccontextmanager
async def _conn(*_a: Any, **_k: Any) -> Any:
    yield MagicMock()


@pytest.fixture
def rig() -> Iterator[Rig]:
    github = FakePullRequest()
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    judge = FakeJudge()
    worker = PRStatusWorker(redis_client=redis, db_engine=MagicMock(), llm_client=judge)
    worker._record_check = AsyncMock()  # type: ignore[method-assign]
    event = MagicMock()
    event.access_control = AccessControl(is_public=True)
    event.source_platform = SourcePlatform.SLACK
    event.source_id = "1623456789.000100"
    event.metadata = {"channel_id": "C12345"}
    store = MagicMock()
    store.get_events_for_context = AsyncMock(return_value=[uuid4()])
    store.get_events_by_ids = AsyncMock(return_value=[event])
    with (
        patch.object(pr_status_reporter, "GitHubRestClient", return_value=github),
        patch.object(pr_status_reporter, "tenant_context", _conn),
        patch.object(pr_status_reporter, "PostgresEventStore", return_value=store),
        patch.object(pr_status_reporter.record_notes, "annotate", AsyncMock()),
        patch.object(
            pr_status_reporter,
            "resolve_repo_audience",
            AsyncMock(return_value=RepoAudience(frozenset({MEMBER}))),
        ),
    ):
        yield Rig(github, redis, worker, judge, [])


async def test_no_records_passes_without_a_comment(rig: Rig) -> None:
    await rig.push()
    assert rig.github.statuses == [("success", "No conflicts with recorded decisions.")]
    assert rig.github.comments == {}
    rig.redis.set.assert_any_call("ci:pr_status:abcdef123456", "completed")
    rig.redis.xack.assert_awaited_once_with("t", "g", "1-0")


async def test_blocking_conflict_fails_with_the_reason_in_the_comment(rig: Rig) -> None:
    rig.candidates = [Candidate(_record("Money is integer cents."), ["quote_total"])]
    rig.judge.verdict = lambda _r, _d: ("conflicts", "quote_total now returns a float.")
    await rig.push()

    assert rig.github.statuses == [
        ("failure", "Conflicts with 1 blocking decision(s). See the Checkcord comment.")
    ]
    body = rig.github.body
    assert body.startswith(COMMENT_MARKER)
    assert "#### Conflicts" in body
    assert "**Blocking**: Money is integer cents." in body
    assert "Reason: quote_total now returns a float." in body
    assert "[Slack thread](https://slack.com/archives/C12345/p1623456789000100)" in body
    assert "`quote_total`" in body
    assert body.endswith("Checked at abcdef1.")
    assert rig.judge.calls == [("Money is integer cents.", "+    return float(total)")]


async def test_conflict_shows_the_latest_update_and_a_pending_replacement(rig: Rig) -> None:
    record = _record("Money is integer cents.")
    record["updates"] = [{"happened_at": "2026-11-02T10:00:00+00:00", "text": "Refunds\ntoo."}]
    record["may_be_replaced_by"] = {
        "record_id": str(uuid4()),
        "created_at": "2026-11-03T09:00:00+00:00",
        "basis": "model",
        "summary": "Money is decimal strings.",
    }
    rig.candidates = [Candidate(record, ["quote_total"])]
    await rig.push()
    assert "  - Latest update (2026-11-02): Refunds too." in rig.github.body
    assert (
        "  - A newer record from 2026-11-03 may replace this; an admin has not confirmed it "
        "yet: Money is decimal strings."
    ) in rig.github.body


async def test_advisory_conflict_passes_with_a_comment(rig: Rig) -> None:
    rig.candidates = [Candidate(_record("Prefer composition.", "soft_warn"), ["Foo.bar"])]
    await rig.push()

    assert rig.github.statuses == [("success", "1 advisory note(s). See the Checkcord comment.")]
    assert "**Advisory**: Prefer composition." in rig.github.body


@pytest.mark.parametrize("verdict", ["complies", "unrelated"])
async def test_complying_or_unrelated_change_passes(rig: Rig, verdict: str) -> None:
    linked = Candidate(_record("Money is integer cents."), ["quote_total"])
    in_scope = Candidate(_record("Never log card numbers."))
    rig.candidates = [linked, in_scope]
    rig.judge.verdict = verdict
    await rig.push()

    assert rig.github.statuses == [("success", "No conflicts with recorded decisions.")]
    body = rig.github.body
    assert "#### Attached to code you changed" in body
    assert "- `quote_total`: Money is integer cents. (blocking" in body
    assert "Never log card numbers" not in body
    assert "Conflicts" not in body


async def test_unrelated_in_scope_record_writes_no_comment(rig: Rig) -> None:
    rig.candidates = [Candidate(_record("Never log card numbers."))]
    rig.judge.verdict = "unrelated"
    await rig.push()
    assert rig.github.state == "success"
    assert rig.github.comments == {}
    assert len(rig.judge.calls) == 1


async def test_restricted_blocking_record_decides_without_its_text(rig: Rig) -> None:
    secret = _record("RESTRICTED-SUMMARY", access={"is_public": False, "allowed_identity_ids": []})
    shared = _record(
        "SHARED-SUMMARY", "soft_warn", {"is_public": False, "allowed_identity_ids": [MEMBER]}
    )
    rig.candidates = [Candidate(shared, ["Shared.fn"]), Candidate(secret, ["Secret.fn"])]
    rig.judge.verdict = lambda text, _d: ("conflicts", f"REASON-FOR {text}")
    await rig.push()

    assert rig.github.state == "failure"
    body = rig.github.body
    assert "SHARED-SUMMARY" in body and "Shared.fn" in body
    assert "RESTRICTED-SUMMARY" not in body
    assert "Secret.fn" not in body
    assert "REASON-FOR RESTRICTED" not in body
    assert "Restricted record (severity: `hard_block`)" in body
    assert f"node=context%3A{secret['id']}" in body
    assert body.count("C12345") == 1


async def test_model_failure_retries_then_errors(rig: Rig) -> None:
    rig.candidates = [Candidate(_record("Money is integer cents."), ["quote_total"])]
    rig.judge.verdict = "fail"
    with patch.object(pr_status_reporter.time, "time", return_value=1000.0):
        await rig.push()
    assert rig.github.statuses[-1][0] == "pending"
    assert rig.github.comments == {}
    key, mapping = rig.redis.zadd.call_args.args
    assert key == RETRY_ZSET
    assert list(mapping.values()) == [1060.0]
    second = rig.retry()
    assert (second.attempt, second.verdicts) == (2, {rig.candidates[0].id: ("failed", "")})

    await rig.push(attempt=2, verdicts=second.verdicts)
    assert rig.retry().attempt == 3
    await rig.push(attempt=3, verdicts=rig.retry().verdicts)

    assert rig.github.statuses[-1] == (
        "error",
        "Could not evaluate 1 blocking decision(s). Push again or re-deliver the webhook to retry.",
    )
    assert [s for s, _ in rig.github.statuses] == ["pending", "pending", "error"]
    assert "#### Could not evaluate" in rig.github.body
    assert len(rig.judge.calls) == 3


async def test_a_retry_that_reaches_the_model_settles_the_check(rig: Rig) -> None:
    done, failed = _record("Use idempotency keys."), _record("Money is integer cents.")
    rig.candidates = [Candidate(done, ["charge"]), Candidate(failed, ["quote_total"])]
    rig.judge.verdict = "complies"
    verdicts = {str(done["id"]): ("complies", "ok"), str(failed["id"]): ("failed", "")}
    await rig.push(attempt=2, verdicts=verdicts)

    assert rig.github.statuses == [("success", "No conflicts with recorded decisions.")]
    assert rig.judge.calls == [("Money is integer cents.", "+    return float(total)")]


async def test_comment_is_updated_in_place_then_cleared(rig: Rig) -> None:
    rig.candidates = [Candidate(_record("Money is integer cents."), ["quote_total"])]
    await rig.push(sha="1111111aaaa")
    await rig.push(sha="2222222bbbb")
    assert (rig.github.created, rig.github.updated) == (1, 1)
    assert rig.github.body.endswith("Checked at 2222222.")

    rig.candidates = [Candidate(_record("Never log card numbers."))]
    rig.judge.verdict = "unrelated"
    await rig.push(sha="3333333cccc")
    assert len(rig.github.comments) == 1
    assert rig.github.body == f"{COMMENT_MARKER}\nNo conflicts with recorded decisions at 3333333."
    assert rig.github.state == "success"


async def test_a_stale_push_does_not_overwrite_the_comment(rig: Rig) -> None:
    rig.candidates = [Candidate(_record("Money is integer cents."), ["quote_total"])]
    rig.redis.get = AsyncMock(return_value=b"newer-sha")
    await rig.push(sha="olderaaaa", attempt=2)
    assert rig.github.comments == {}
    assert rig.github.state == "failure"


async def test_own_branch_is_not_checked(rig: Rig) -> None:
    rig.candidates = [Candidate(_record("Money is integer cents."), ["quote_total"])]
    await rig.push(head_ref="checkcord/update-context")
    assert rig.github.statuses == [("success", "Checkcord's own update; not checked.")]
    assert rig.github.comments == {}
    assert rig.judge.calls == []


async def test_own_branch_check_is_recorded_as_skipped(rig: Rig) -> None:
    await rig.push(head_ref="checkcord/update-context")
    assert rig.worker._record_check.await_args.kwargs["skipped"] is True


async def test_judge_usage_is_written_before_the_status_is_posted(rig: Rig) -> None:
    rig.candidates = [Candidate(_record("Money is integer cents."), ["quote_total"])]
    rig.judge.verdict = lambda _r, _d: ("complies", "fine")
    posted_at_flush: list[int] = []

    async def flush(_engine: Any) -> None:
        posted_at_flush.append(len(rig.github.statuses))

    with patch.object(pr_status_reporter.usage, "flush", flush):
        await rig.push()

    assert posted_at_flush == [0]
    assert rig.github.statuses[-1][0] == "success"


async def test_touch_mode_attaches_without_a_model(rig: Rig) -> None:
    rig.worker.settings = rig.worker.settings.model_copy(update={"pr_check_mode": "touch"})
    rig.candidates = [Candidate(_record("Money is integer cents."), ["quote_total"])]
    await rig.push()

    assert rig.github.statuses == [
        (
            "failure",
            "1 blocking decision(s) attached to changed code. See the Checkcord comment.",
        )
    ]
    body = rig.github.body
    assert "#### Records attached to code this pull request changes" in body
    assert "violat" not in body.lower()
    assert "Reason:" not in body
    assert rig.judge.calls == []


async def test_no_db_engine_is_not_evaluated() -> None:
    github = FakePullRequest()
    with patch.object(pr_status_reporter, "GitHubRestClient", return_value=github):
        worker = PRStatusWorker(redis_client=AsyncMock())
        payload = PRContextCheckedPayload(
            repository_full_name="owner/repo",
            pull_request_number=42,
            head_sha="abc",
            tenant_id=uuid4(),
        )
        await worker._process_message("t", "g", "1-0", _wire_payload(payload))
    assert github.statuses == [("error", pr_status_reporter.NOT_EVALUATED)]


async def test_due_retries_are_requeued_once() -> None:
    redis = AsyncMock()
    redis.zrangebyscore = AsyncMock(return_value=[b'{"x": 1}'])
    redis.zrem = AsyncMock(side_effect=[1, 0])
    worker = PRStatusWorker(redis_client=redis)
    await worker._requeue_due_retries()
    await worker._requeue_due_retries()
    redis.xadd.assert_awaited_once_with("ci.pr.context_checked", fields={"payload": '{"x": 1}'})


@patch("checkcord.workers.pr_status_reporter.GitHubRestClient")
async def test_pr_status_worker_fallback_polling(MockGitHubRestClient):
    client_mock = MockGitHubRestClient.return_value
    client_mock.create_commit_status = AsyncMock(return_value=True)

    redis_mock = AsyncMock()
    # Mock ZSET containing one expired item
    fallback_data = {
        "owner": "owner",
        "repo": "repo",
        "sha": "abcdef123456",
        "pr_number": 42,
        "tenant_id": str(uuid4()),
    }
    fallback_bytes = json.dumps(fallback_data).encode("utf-8")

    redis_mock.zrangebyscore = AsyncMock(side_effect=_due(fallback_bytes))

    redis_mock.get = AsyncMock(return_value=b"pending")

    worker = PRStatusWorker(redis_client=redis_mock)

    # Run a single step of the fallback check loop by canceling after a brief run
    with (
        patch("checkcord.workers.pr_status_reporter.time.time", return_value=100.0),
        patch(
            "checkcord.workers.pr_status_reporter.asyncio.sleep",
            side_effect=asyncio.CancelledError,
        ),
    ):
        try:
            await worker._poll_fallback_tasks()
        except asyncio.CancelledError:
            pass

    client_mock.create_commit_status.assert_called_once_with(
        owner="owner",
        repo="repo",
        sha="abcdef123456",
        state="error",
        description=pr_status_reporter.NOT_EVALUATED,
    )
    # Should have updated status in Redis
    redis_mock.set.assert_called_once_with("ci:pr_status:abcdef123456", "completed")
    # Should have removed item from ZSET
    redis_mock.zrem.assert_called_once_with("ci.pr.fallback_zset", fallback_bytes.decode("utf-8"))


@patch("checkcord.workers.pr_status_reporter.GitHubRestClient")
async def test_pr_status_worker_fallback_polling_completed(MockGitHubRestClient):
    client_mock = MockGitHubRestClient.return_value
    client_mock.create_commit_status = AsyncMock()

    redis_mock = AsyncMock()
    fallback_data = {
        "owner": "owner",
        "repo": "repo",
        "sha": "abcdef123456",
        "pr_number": 42,
        "tenant_id": str(uuid4()),
    }
    fallback_bytes = json.dumps(fallback_data).encode("utf-8")

    redis_mock.zrangebyscore = AsyncMock(side_effect=_due(fallback_bytes))

    # 2. Test case when status is "completed" (should NOT trigger fail-open)
    redis_mock.get = AsyncMock(return_value=b"completed")

    worker = PRStatusWorker(redis_client=redis_mock)

    with (
        patch("checkcord.workers.pr_status_reporter.time.time", return_value=100.0),
        patch(
            "checkcord.workers.pr_status_reporter.asyncio.sleep",
            side_effect=asyncio.CancelledError,
        ),
    ):
        try:
            await worker._poll_fallback_tasks()
        except asyncio.CancelledError:
            pass

    # Should NOT have updated commit status on GitHub
    client_mock.create_commit_status.assert_not_called()
    # Should have removed it from the ZSET anyway to clean up
    redis_mock.zrem.assert_called_once_with("ci.pr.fallback_zset", fallback_bytes.decode("utf-8"))


@patch("checkcord.workers.pr_status_reporter.GitHubRestClient")
async def test_pr_status_worker_dlq_entry_has_error_field(MockGitHubRestClient):

    client_mock = MockGitHubRestClient.return_value
    client_mock.create_commit_status = AsyncMock(side_effect=Exception("GitHub API down"))

    redis_mock = AsyncMock()
    worker = PRStatusWorker(redis_client=redis_mock)

    payload = _wire_payload(
        PRContextCheckedPayload(
            repository_full_name="owner/repo",
            pull_request_number=42,
            head_sha="abcdef123456",
            tenant_id=uuid4(),
            affected_contexts=[],  # empty -> hits the simple create_commit_status branch
        )
    )

    await worker._process_message("ci.pr.context_checked", "group", "12345-0", payload)
    redis_mock.xadd.assert_not_called()
    retry = json.loads(next(iter(redis_mock.zadd.call_args.args[1])))
    assert retry["attempt"] == 2

    retry["attempt"] = 3
    await worker._process_message(
        "ci.pr.context_checked", "group", "12346-0", {"payload": json.dumps(retry)}
    )
    redis_mock.xadd.assert_called_once()
    call_args = redis_mock.xadd.call_args
    assert call_args.kwargs["name"] == "ci.pr.context_checked.dlq"
    fields = call_args.kwargs["fields"]
    assert "GitHub API down" in fields["dlq_error"]
    assert set(fields) == {"payload", "dlq_error"}


@patch("checkcord.workers.pr_status_reporter.GitHubRestClient")
async def test_status_token_comes_from_lookup(MockGitHubRestClient, token_lookup):
    MockGitHubRestClient.return_value.create_commit_status = AsyncMock(return_value=True)
    redis_mock = AsyncMock()
    tenant_id = uuid4()
    worker = PRStatusWorker(redis_client=redis_mock)

    body = json.loads(
        PRContextCheckedPayload(
            repository_full_name="owner/repo",
            pull_request_number=42,
            head_sha="abc",
            tenant_id=tenant_id,
            affected_contexts=[],
        ).model_dump_json()
    )
    body["personal_access_token"] = "ghp_stale_in_flight"
    await worker._process_message("t", "g", "1-0", {"payload": json.dumps(body)})

    token_lookup.assert_awaited_once_with(None, tenant_id, "owner/repo")
    MockGitHubRestClient.assert_called_once_with("mock-pat")
    redis_mock.xack.assert_awaited_once_with("t", "g", "1-0")


@patch("checkcord.workers.pr_status_reporter.GitHubRestClient")
async def test_status_dlq_copy_drops_legacy_token(MockGitHubRestClient):
    MockGitHubRestClient.return_value.create_commit_status = AsyncMock(side_effect=Exception("x"))
    redis_mock = AsyncMock()
    worker = PRStatusWorker(redis_client=redis_mock)

    body = json.loads(
        PRContextCheckedPayload(
            repository_full_name="owner/repo",
            pull_request_number=42,
            head_sha="abc",
            tenant_id=uuid4(),
            affected_contexts=[],
        ).model_dump_json()
    )
    body["personal_access_token"] = "ghp_stale_in_flight"
    body["attempt"] = 3
    await worker._process_message("t", "g", "1-0", {"payload": json.dumps(body)})

    fields = redis_mock.xadd.call_args.kwargs["fields"]
    assert "ghp_stale_in_flight" not in str(fields)
    assert json.loads(fields["payload"])["repository_full_name"] == "owner/repo"


@patch("checkcord.workers.pr_status_reporter.GitHubRestClient")
async def test_fallback_looks_up_token_and_ignores_legacy_pat(MockGitHubRestClient, token_lookup):
    MockGitHubRestClient.return_value.create_commit_status = AsyncMock(return_value=True)
    redis_mock = AsyncMock()
    tenant_id = uuid4()
    item = json.dumps(
        {
            "owner": "owner",
            "repo": "repo",
            "sha": "abc",
            "pat": "ghp_stale_in_flight",
            "pr_number": 1,
            "tenant_id": str(tenant_id),
        }
    )
    redis_mock.zrangebyscore = AsyncMock(side_effect=_due(item.encode()))
    redis_mock.get = AsyncMock(return_value=b"pending")
    worker = PRStatusWorker(redis_client=redis_mock, db_engine=MagicMock())

    with patch(
        "checkcord.workers.pr_status_reporter.asyncio.sleep",
        side_effect=asyncio.CancelledError,
    ):
        try:
            await worker._poll_fallback_tasks()
        except asyncio.CancelledError:
            pass

    token_lookup.assert_awaited_once_with(worker.db_engine, tenant_id, "owner/repo")
    MockGitHubRestClient.assert_called_once_with("mock-pat")
    redis_mock.zrem.assert_awaited_once_with("ci.pr.fallback_zset", item)


def _payload(sha: str = "abc", **fields: Any) -> PRContextCheckedPayload:
    return PRContextCheckedPayload(
        repository_full_name="owner/repo",
        pull_request_number=7,
        head_sha=sha,
        tenant_id=uuid4(),
        **{"diff": "+x", **fields},
    )


async def _run_attempts(rig: Rig, sha: str) -> None:
    attempt = 1
    while True:
        rig.redis.zadd.reset_mock()
        await rig.worker._process_message(
            "t", "g", "1-0", _wire_payload(_payload(sha, attempt=attempt))
        )
        if not rig.redis.zadd.await_count:
            return
        attempt = rig.retry().attempt


async def test_an_exception_while_checking_retries_then_errors(rig: Rig) -> None:
    rig.worker._candidates = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    await _run_attempts(rig, "e1")
    states = [s for s, _ in rig.github.statuses]
    assert states == ["pending", "pending", "error"]
    assert rig.github.statuses[-1] == ("error", pr_status_reporter.NOT_EVALUATED)


async def test_a_judge_timeout_ends_in_error(rig: Rig) -> None:
    rig.candidates = [Candidate(_record("Money is integer cents."), ["quote_total"])]

    def slow(_r: str, _d: str) -> tuple[str, str]:
        raise TimeoutError("model timed out")

    rig.judge.verdict = slow
    rig.worker._candidates = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda *_a, **_k: (
            [Candidate(c.record, list(c.symbols)) for c in rig.candidates],
            0,
        )
    )
    await _run_attempts(rig, "t1")
    assert rig.github.statuses[-1][0] == "error"
    assert all(s != "success" for s, _ in rig.github.statuses)


async def test_error_can_be_configured_to_pass(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rig.worker.settings, "pr_check_on_error", "success")
    monkeypatch.setenv("PR_CHECK_ON_ERROR", "success")
    pr_status_reporter.get_settings.cache_clear()
    rig.worker._candidates = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    await _run_attempts(rig, "s1")
    assert rig.github.statuses[-1] == ("success", "Not evaluated: internal error.")


async def test_five_pull_requests_together_all_get_a_verdict(rig: Rig) -> None:
    rig.candidates = [Candidate(_record("Money is integer cents."), ["quote_total"])]
    rig.judge.verdict = lambda _r, d: ("conflicts", "float") if "float" in d else ("complies", "")
    rig.worker._candidates = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda *_a, **_k: (
            [Candidate(c.record, list(c.symbols)) for c in rig.candidates],
            0,
        )
    )
    payloads = [
        _payload(f"sha{n}", diff="+    return float(total)" if n % 2 else "+ok") for n in range(5)
    ]
    await asyncio.gather(
        *(
            rig.worker._process_message("t", "g", f"{n}-0", _wire_payload(p))
            for n, p in enumerate(payloads)
        )
    )
    assert sorted(s for s, _ in rig.github.statuses) == ["failure"] * 2 + ["success"] * 3
    assert all("Not evaluated" not in d for _, d in rig.github.statuses)


@patch("checkcord.workers.pr_status_reporter.GitHubRestClient")
async def test_watchdog_never_passes_an_unchecked_change(MockGitHubRestClient):
    MockGitHubRestClient.return_value.create_commit_status = AsyncMock(return_value=True)
    redis_mock = AsyncMock()
    item = json.dumps(
        {"owner": "o", "repo": "r", "sha": "abc", "pr_number": 3, "tenant_id": str(uuid4())}
    )
    redis_mock.zrangebyscore = AsyncMock(side_effect=_due(item.encode()))
    redis_mock.get = AsyncMock(return_value=b"checking")
    worker = PRStatusWorker(redis_client=redis_mock)
    worker._record_check = AsyncMock()  # type: ignore[method-assign]
    with patch(
        "checkcord.workers.pr_status_reporter.asyncio.sleep", side_effect=asyncio.CancelledError
    ):
        try:
            await worker._poll_fallback_tasks()
        except asyncio.CancelledError:
            pass
    kwargs = MockGitHubRestClient.return_value.create_commit_status.call_args.kwargs
    assert kwargs["state"] == "error"
    assert worker._record_check.await_args.args[3] == "error"
