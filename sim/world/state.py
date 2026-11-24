"""What the stand-in providers hold and what they received."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Received:
    seq: int
    service: str
    method: str
    path: str
    query: str
    digest: str
    body: str
    status: int
    at: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()[:16]


@dataclass
class Log:
    entries: list[Received] = field(default_factory=list)
    max_body: int = 20_000

    def add(
        self, service: str, method: str, path: str, query: str, body: bytes, status: int
    ) -> Received:
        text = body.decode("utf-8", "replace")
        entry = Received(
            len(self.entries) + 1,
            service,
            method,
            path,
            query,
            digest(body),
            text[: self.max_body],
            status,
            time.time(),
        )
        self.entries.append(entry)
        return entry

    def since(self, seq: int, service: str | None = None) -> list[Received]:
        return [
            e for e in self.entries if e.seq > seq and (service is None or e.service == service)
        ]

    @property
    def last(self) -> int:
        return len(self.entries)


@dataclass
class SlackMessage:
    channel: str
    ts: str
    user: str | None
    text: str
    thread_ts: str | None = None
    subtype: str | None = None
    bot_id: str | None = None
    username: str | None = None
    files: list[dict[str, Any]] = field(default_factory=list)
    edited_ts: str | None = None
    deleted: bool = False
    reply_count: int = 0

    def wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "message", "ts": self.ts, "text": self.text}
        if self.user:
            out["user"] = self.user
        if self.thread_ts:
            out["thread_ts"] = self.thread_ts
        if self.subtype:
            out["subtype"] = self.subtype
        if self.bot_id:
            out["bot_id"] = self.bot_id
        if self.username:
            out["username"] = self.username
        if self.files:
            out["files"] = self.files
        if self.edited_ts:
            out["edited"] = {"user": self.user, "ts": self.edited_ts}
        if self.reply_count:
            out["reply_count"] = self.reply_count
        return out


@dataclass
class SlackChannel:
    id: str
    name: str
    private: bool
    members: list[str]
    created: int = field(default_factory=lambda: int(time.time()) - 86400 * 400)


@dataclass
class SlackUser:
    id: str
    name: str
    real_name: str
    email: str
    deleted: bool = False
    is_bot: bool = False


@dataclass
class GitCommit:
    sha: str
    tree: dict[str, str]
    parent: str | None
    message: str
    author: str
    at: float


@dataclass
class PullRequest:
    number: int
    title: str
    body: str
    author: str
    head_ref: str
    base_ref: str
    head_sha: str
    base_sha: str
    state: str = "open"
    merged: bool = False
    merge_commit_sha: str | None = None
    created_at: float = field(default_factory=time.time)
    merged_at: float | None = None
    by_checkcord: bool = False


@dataclass
class GitRepo:
    full_name: str
    private: bool
    default_branch: str
    collaborators: list[str]
    commits: dict[str, GitCommit] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    pulls: dict[int, PullRequest] = field(default_factory=dict)
    statuses: list[dict[str, Any]] = field(default_factory=list)
    comments: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    content_writes: list[dict[str, Any]] = field(default_factory=list)
    webhook_url: str | None = None
    webhook_secret: str | None = None
    next_number: int = 1


@dataclass
class JiraIssue:
    key: str
    id: str
    project: str
    summary: str
    description: str
    issuetype: str
    status: str
    reporter: str
    created: float
    updated: float
    comments: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ModelState:
    mode: str = "ok"
    calls: dict[str, int] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    bodies: list[dict[str, Any]] = field(default_factory=list)

    def body_texts(self, since: int = 0) -> list[str]:
        return [json.dumps(b["body"]) for b in self.bodies[since:]]


@dataclass
class Deliveries:
    items: list[dict[str, Any]] = field(default_factory=list)

    def add(self, **item: Any) -> dict[str, Any]:
        item["seq"] = len(self.items) + 1
        item["at"] = time.time()
        self.items.append(item)
        return item
