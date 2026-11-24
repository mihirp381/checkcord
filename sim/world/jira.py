"""Jira stand-in: the REST search and issue routes, and signed webhook deliveries."""

from __future__ import annotations

import json
import time
from typing import Any

import aiohttp
from aiohttp import web

from sim import company
from sim.world.github import sign
from sim.world.state import Deliveries, JiraIssue

PAGE = 2


def _iso(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000+0000", time.gmtime(at))


def _adf(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


class Jira:
    def __init__(self, host: str, token: str, deliveries: Deliveries) -> None:
        self.host = host
        self.token = token
        self.deliveries = deliveries
        self.issues: dict[str, JiraIssue] = {}
        self.webhook_url: str | None = None
        self.webhook_secret: str | None = None
        self.extra_hooks: list[tuple[str, str]] = []
        self.project_hosts: dict[str, str] = {}
        self._id = 10_000
        self._delivery = 0
        self.last_sent: tuple[bytes, str] | None = None

    def routes(self, app: web.Application) -> None:
        app.router.add_get("/rest/api/3/search", self.search)
        app.router.add_get("/rest/api/3/issue/{key}", self.get_issue)
        app.router.add_get("/rest/api/3/issue/{key}/comment", self.get_comments)

    def _ok(self, request: web.Request) -> bool:
        return request.headers.get("Authorization", "") in (
            f"Bearer {self.token}",
            f"Basic {self.token}",
        )

    def _user(self, key: str) -> dict[str, Any]:
        p = company.person(key)
        return {
            "accountId": f"acc-{p.key}",
            "displayName": p.name,
            "emailAddress": p.email,
            "active": True,
        }

    def _host(self, issue: JiraIssue) -> str:
        return self.project_hosts.get(issue.project, self.host)

    def _wire(self, issue: JiraIssue) -> dict[str, Any]:
        return {
            "id": issue.id,
            "key": issue.key,
            "self": f"https://{self._host(issue)}/rest/api/3/issue/{issue.id}",
            "fields": {
                "summary": issue.summary,
                "description": _adf(issue.description),
                "status": {"name": issue.status},
                "issuetype": {"id": "10001", "name": issue.issuetype, "subtask": False},
                "project": {"key": issue.project, "name": issue.project},
                "reporter": self._user(issue.reporter),
                "assignee": None,
                "created": _iso(issue.created),
                "updated": _iso(issue.updated),
                "comment": {"comments": issue.comments, "total": len(issue.comments)},
            },
        }

    async def search(self, request: web.Request) -> web.Response:
        if not self._ok(request):
            return web.json_response({"errorMessages": ["unauthorized"]}, status=401)
        start = int(request.query.get("startAt", "0"))
        size = min(int(request.query.get("maxResults", "50")), PAGE)
        rows = sorted(self.issues.values(), key=lambda i: (i.updated, i.key))
        chunk = rows[start : start + size]
        return web.json_response(
            {
                "startAt": start,
                "maxResults": size,
                "total": len(rows),
                "issues": [self._wire(i) for i in chunk],
            }
        )

    async def get_issue(self, request: web.Request) -> web.Response:
        if not self._ok(request):
            return web.json_response({"errorMessages": ["unauthorized"]}, status=401)
        issue = self.issues.get(request.match_info["key"])
        if issue is None:
            return web.json_response({"errorMessages": ["Issue does not exist"]}, status=404)
        return web.json_response(self._wire(issue))

    async def get_comments(self, request: web.Request) -> web.Response:
        if not self._ok(request):
            return web.json_response({"errorMessages": ["unauthorized"]}, status=401)
        issue = self.issues.get(request.match_info["key"])
        if issue is None:
            return web.json_response({"errorMessages": ["Issue does not exist"]}, status=404)
        return web.json_response(
            {"startAt": 0, "total": len(issue.comments), "comments": issue.comments}
        )

    async def _send(
        self, payload: dict[str, Any], again: tuple[bytes, str] | None = None
    ) -> dict[str, Any]:
        if again is None:
            self._delivery += 1
        body, ident = again or (json.dumps(payload).encode(), f"delivery-{self._delivery}")
        self.last_sent = (body, ident)
        status, text = 0, ""
        also = []
        hooks = [(self.webhook_url, self.webhook_secret), *self.extra_hooks]
        for n, (url, secret) in enumerate(hooks):
            if not url or not secret:
                continue
            headers = {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sign(secret, body),
                "X-Atlassian-Webhook-Identifier": ident,
            }
            got, said = 0, ""
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(url, data=body, headers=headers) as resp:
                        got, said = resp.status, await resp.text()
            except aiohttp.ClientError as err:
                said = str(err)
            if n == 0:
                status, text = got, said
            else:
                also.append({"status": got, "response": said[:300]})
        return self.deliveries.add(
            service="jira",
            event=payload["webhookEvent"],
            key=(payload.get("issue") or {}).get("key"),
            status=status,
            response=text[:500],
            identifier=ident,
            also=also,
            redelivery=again is not None,
        )

    async def redeliver(self) -> dict[str, Any]:
        if self.last_sent is None:
            raise KeyError("nothing sent yet")
        return await self._send(json.loads(self.last_sent[0]), again=self.last_sent)

    async def create(
        self,
        key: str,
        summary: str,
        description: str,
        reporter: str,
        issuetype: str = "Task",
        at: float | None = None,
    ) -> dict[str, Any]:
        self._id += 1
        now = at or time.time()
        issue = JiraIssue(
            key,
            str(self._id),
            key.split("-")[0],
            summary,
            description,
            issuetype,
            "To Do",
            reporter,
            now,
            now,
        )
        self.issues[key] = issue
        return await self._send(
            {
                "timestamp": int(now * 1000),
                "webhookEvent": "jira:issue_created",
                "issue_event_type_name": "issue_created",
                "user": self._user(reporter),
                "issue": self._wire(issue),
            }
        )

    async def update(
        self, key: str, by: str, summary: str | None = None, description: str | None = None
    ) -> dict[str, Any]:
        issue = self.issues[key]
        items = []
        if summary is not None:
            items.append({"field": "summary", "fromString": issue.summary, "toString": summary})
            issue.summary = summary
        if description is not None:
            items.append({"field": "description", "fromString": "", "toString": description})
            issue.description = description
        issue.updated = time.time()
        return await self._send(
            {
                "timestamp": int(issue.updated * 1000),
                "webhookEvent": "jira:issue_updated",
                "issue_event_type_name": "issue_generic",
                "user": self._user(by),
                "issue": self._wire(issue),
                "changelog": {"id": str(self._id), "items": items},
            }
        )

    async def comment(self, key: str, by: str, text: str) -> dict[str, Any]:
        issue = self.issues[key]
        self._id += 1
        now = time.time()
        row = {
            "id": str(self._id),
            "self": f"https://{self._host(issue)}/rest/api/3/issue/{issue.id}/comment/{self._id}",
            "body": _adf(text),
            "author": self._user(by),
            "created": _iso(now),
            "updated": _iso(now),
        }
        issue.comments.append(row)
        issue.updated = now
        return await self._send(
            {
                "timestamp": int(now * 1000),
                "webhookEvent": "comment_created",
                "issue": self._wire(issue),
                "comment": row,
            }
        )
