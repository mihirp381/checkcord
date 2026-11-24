"""Linear stand-in: OAuth install and the GraphQL issue and comment queries, newest first."""

from __future__ import annotations

import secrets
import time
import uuid
from datetime import datetime
from typing import Any

from aiohttp import web


def _iso(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(at)) + f".{int(at * 1000) % 1000:03d}Z"


class Linear:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.org_id = str(uuid.uuid4())
        self.teams: dict[str, str] = {}
        self.codes: dict[str, str] = {}
        self.tokens: set[str] = set()
        self.issues: dict[str, dict[str, Any]] = {}
        self.queries: list[dict[str, Any]] = []
        self._n: dict[str, int] = {}

    def routes(self, app: web.Application) -> None:
        app.router.add_get("/linear/oauth/authorize", self.authorize)
        app.router.add_post("/linear/oauth/token", self.token)
        app.router.add_post("/linear/graphql", self.graphql)

    async def authorize(self, request: web.Request) -> web.StreamResponse:
        q = request.query
        if q.get("client_id") != self.client_id:
            return web.json_response({"error": "invalid_client"}, status=400)
        code = secrets.token_urlsafe(16)
        self.codes[code] = q.get("redirect_uri", "")
        raise web.HTTPFound(f"{self.codes[code]}?code={code}&state={q.get('state', '')}")

    async def token(self, request: web.Request) -> web.Response:
        form = await request.post()
        ok = (
            form.get("client_id") == self.client_id
            and form.get("client_secret") == self.client_secret
            and form.get("code") in self.codes
        )
        if not ok:
            return web.json_response({"error": "invalid_grant"}, status=400)
        self.codes.pop(str(form["code"]))
        access = "lin_oauth_" + secrets.token_hex(20)
        self.tokens.add(access)
        return web.json_response(
            {"access_token": access, "token_type": "Bearer", "expires_in": 315_705_599}
        )

    def add_issue(
        self,
        team: str,
        title: str,
        description: str,
        comments: list[tuple[str, str]] | None = None,
        at: float | None = None,
    ) -> dict[str, Any]:
        team_id = self.teams.setdefault(team, str(uuid.uuid4()))
        self._n[team] = self._n.get(team, 0) + 1
        issue_id = str(uuid.uuid4())
        issue_time = _iso(at if at is not None else time.time())
        issue: dict[str, Any] = {
            "id": issue_id,
            "identifier": f"{team}-{self._n[team]}",
            "title": title,
            "updatedAt": _iso(at if at is not None else time.time()),
            "team": {"id": team_id, "key": team},
            "description": description,
            "priority": 2,
            "state": {"name": "Todo"},
            "comments": [
                {
                    "id": str(uuid.uuid4()),
                    "body": body,
                    "user": {"name": name},
                    "createdAt": issue_time,
                    "updatedAt": issue_time,
                }
                for name, body in comments or []
            ],
        }
        self.issues[issue_id] = issue
        return issue

    def _wire(self, issue: dict[str, Any]) -> dict[str, Any]:
        return {
            **issue,
            "comments": {
                "nodes": issue["comments"],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }

    async def graphql(self, request: web.Request) -> web.Response:
        if request.headers.get("Authorization", "").removeprefix("Bearer ") not in self.tokens:
            return web.json_response(
                {"errors": [{"message": "Authentication required"}]}, status=401
            )
        b = await request.json()
        query, variables = b.get("query", ""), b.get("variables") or {}
        self.queries.append({"query": query.split("(")[0].strip()[:60], "variables": variables})
        if "viewer" in query:
            return web.json_response({"data": {"viewer": {"organization": {"id": self.org_id}}}})
        if "GetComments" in query:
            issue = self.issues.get(variables.get("id", ""))
            nodes = issue["comments"] if issue else []
            return web.json_response(
                {
                    "data": {
                        "issue": {
                            "comments": {
                                "nodes": nodes,
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                }
            )
        since = str(variables.get("since") or "")
        rows = sorted(
            (i for i in self.issues.values() if _after(i["updatedAt"], since)),
            key=lambda i: i["updatedAt"],
            reverse=True,
        )
        ids = [i["id"] for i in rows]
        if variables.get("after") in ids:
            rows = rows[ids.index(variables["after"]) + 1 :]
        if variables.get("before") in ids:
            rows = rows[: ids.index(variables["before"])]
        first, last = variables.get("first"), variables.get("last")
        if first:
            nodes, has_next, has_prev = rows[:first], len(rows) > first, False
        elif last:
            nodes, has_next, has_prev = rows[-last:], False, len(rows) > last
        else:
            nodes, has_next, has_prev = rows[:50], len(rows) > 50, False
        info: dict[str, Any] = {"hasNextPage": has_next, "hasPreviousPage": has_prev}
        if nodes:
            info.update(startCursor=nodes[0]["id"], endCursor=nodes[-1]["id"])
        return web.json_response(
            {"data": {"issues": {"nodes": [self._wire(i) for i in nodes], "pageInfo": info}}}
        )


def _after(updated: str, since: str) -> bool:
    if not since:
        return True
    a = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    b = datetime.fromisoformat(since.replace("Z", "+00:00"))
    return a > b
