from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeJudge:
    """Stands in for the model that judges a diff against a record."""

    verdict: Callable[[str, str], tuple[str, str]] | str = "conflicts"
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def judge_change(self, record_text: str, diff: str) -> tuple[str, str]:
        self.calls.append((record_text, diff))
        if callable(self.verdict):
            return self.verdict(record_text, diff)
        if self.verdict == "fail":
            raise RuntimeError("model unreachable")
        return self.verdict, f"The diff {self.verdict} with the decision."


@dataclass
class FakePullRequest:
    """The GitHub calls the pull-request check makes, kept in memory."""

    comments: dict[int, str] = field(default_factory=dict)
    statuses: list[tuple[str, str]] = field(default_factory=list)
    created: int = 0
    updated: int = 0

    async def list_issue_comments(self, owner: str, repo: str, n: int) -> list[dict[str, Any]]:
        return [{"id": i, "body": b} for i, b in self.comments.items()]

    async def update_issue_comment(self, owner: str, repo: str, cid: int, body: str) -> bool:
        self.comments[cid] = body
        self.updated += 1
        return True

    async def create_issue_comment(self, **kwargs: Any) -> bool:
        self.comments[len(self.comments) + 1] = kwargs["body"]
        self.created += 1
        return True

    async def create_commit_status(self, **kwargs: Any) -> bool:
        self.statuses.append((kwargs["state"], kwargs["description"]))
        return True

    @property
    def body(self) -> str:
        return list(self.comments.values())[-1]

    @property
    def state(self) -> str:
        return self.statuses[-1][0]
