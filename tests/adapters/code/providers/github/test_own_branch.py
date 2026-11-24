from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from checkcord.adapters.code.providers.github import parser
from checkcord.adapters.code.providers.github.schemas import (
    GitHubIssueCommentPayload,
    GitHubPRPayload,
    GitHubPRReviewCommentPayload,
    GitHubPRReviewPayload,
    GitHubPushPayload,
)

REPO = {"full_name": "marrowfield/payments-api", "private": True}
USER = {"login": "tomas"}
WHEN = "2026-11-20T10:00:00Z"


def _pr(head: str) -> dict[str, Any]:
    return {
        "number": 9,
        "title": "Update context",
        "created_at": WHEN,
        "html_url": "https://github.com/marrowfield/payments-api/pull/9",
        "user": USER,
        "head": {"ref": head, "sha": "abc"},
        "base": {"ref": "main"},
    }


def _comment() -> dict[str, Any]:
    return {"id": 5, "body": "Looks right.", "created_at": WHEN, "user": USER}


async def _parsed(head: str) -> list[Any]:
    engine, tenant = MagicMock(), uuid4()
    pull = {"action": "opened", "number": 9, "pull_request": _pr(head), "repository": REPO}
    review = {
        "action": "submitted",
        "review": {"id": 3, "state": "approved", "submitted_at": WHEN, "user": USER},
        "pull_request": _pr(head),
        "repository": REPO,
    }
    review_comment = {
        "action": "created",
        "comment": _comment(),
        "pull_request": _pr(head),
        "repository": REPO,
    }
    issue_comment = {
        "action": "created",
        "comment": _comment(),
        "issue": {"number": 9, "pull_request": {"html_url": _pr(head)["html_url"]}},
        "repository": REPO,
    }
    push = {
        "ref": f"refs/heads/{head}",
        "before": "000",
        "after": "abc",
        "commits": [
            {
                "id": "abc",
                "message": "Update ARCHITECTURE.md",
                "timestamp": WHEN,
                "author": {"name": "Checkcord", "email": "bot@example.com"},
            }
        ],
        "repository": REPO,
    }
    return [
        await parser.parse_pull_request_event(
            GitHubPRPayload.model_validate(pull), engine, None, tenant, "pat"
        ),
        await parser.parse_pr_review_event(
            GitHubPRReviewPayload.model_validate(review), engine, None, tenant, "pat"
        ),
        await parser.parse_pr_review_comment_event(
            GitHubPRReviewCommentPayload.model_validate(review_comment), engine, None, tenant, "pat"
        ),
        await parser.parse_issue_comment_event(
            GitHubIssueCommentPayload.model_validate(issue_comment), engine, None, tenant, "pat"
        ),
        await parser.parse_push_event(
            GitHubPushPayload.model_validate(push), engine, None, tenant, "pat"
        ),
    ]


@pytest.mark.asyncio
async def test_nothing_on_checkcords_own_branches_is_ingested() -> None:
    pull = AsyncMock(return_value={"head": {"ref": "checkcord/update-context"}})
    with patch.object(parser.GitHubRestClient, "get_pull_request", pull):
        assert await _parsed("checkcord/update-context") == [None, None, None, None, []]
    pull.assert_awaited_once_with("marrowfield", "payments-api", 9)


@pytest.mark.asyncio
async def test_other_branches_are_still_parsed() -> None:
    reached: list[str] = []

    async def acl(*args: Any) -> Any:
        reached.append("acl")
        raise RuntimeError("stop here")

    pull = AsyncMock(return_value={"head": {"ref": "feature/checkcord/x"}})
    with (
        patch.object(parser.GitHubRestClient, "get_pull_request", pull),
        patch.object(parser, "resolve_github_user", AsyncMock(return_value=(None, None, None))),
        patch.object(parser, "get_repository_acl", acl),
        pytest.raises(RuntimeError),
    ):
        await parser.parse_issue_comment_event(
            GitHubIssueCommentPayload.model_validate(
                {
                    "action": "created",
                    "comment": _comment(),
                    "issue": {"number": 9, "pull_request": {"html_url": "u"}},
                    "repository": REPO,
                }
            ),
            MagicMock(),
            None,
            uuid4(),
            "pat",
        )
    assert reached == ["acl"]


def test_own_branch_reads_refs_and_branch_names() -> None:
    assert parser.own_branch("refs/heads/checkcord/deprecate-stale-context")
    assert parser.own_branch("checkcord/update-context")
    assert not parser.own_branch("refs/heads/feature/checkcord")
    assert not parser.own_branch(None)
