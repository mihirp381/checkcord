"""How the simulation talks to Checkcord and to the world: as people, admins, SCIM and the CLI."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

ROOT = Path(__file__).resolve().parent.parent
API_PORT = 8410
WEB_PORT = 3410
API = f"http://localhost:{API_PORT}"
WEB = f"http://localhost:{WEB_PORT}"
SCIM_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_ENTERPRISE = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
SCIM_PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


@dataclass
class Reply:
    status: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        return self.body if isinstance(self.body, str) else json.dumps(self.body, default=str)


def _reply(resp: httpx.Response) -> Reply:
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text
    return Reply(resp.status_code, body, dict(resp.headers))


class Calls:
    """Every request the harness made to Checkcord, for the per-scenario record."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, who: str, method: str, path: str, status: int) -> None:
        self.items.append(
            {
                "at": round(time.time(), 3),
                "who": who,
                "method": method,
                "path": path,
                "status": status,
            }
        )


class Base:
    def __init__(self, http: httpx.AsyncClient, calls: Calls, who: str) -> None:
        self.http = http
        self.calls = calls
        self.who = who

    def headers(self) -> dict[str, str]:
        return {}

    async def call(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Reply:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        resp = await self.http.request(
            method,
            API + path,
            json=json_body,
            params=clean or None,
            headers={**self.headers(), **(headers or {})},
        )
        self.calls.add(self.who, method, path, resp.status_code)
        return _reply(resp)

    async def get(self, path: str, **params: Any) -> Reply:
        return await self.call("GET", path, params=params)

    async def post(self, path: str, body: Any = None, **params: Any) -> Reply:
        return await self.call(
            "POST", path, json_body=body if body is not None else {}, params=params
        )

    async def graph(self, kind: str, **params: Any) -> Reply:
        return await self.get(f"/api/v1/graph/{kind}", **params)

    async def node(self, node_id: str, repo: str | None = None) -> Reply:
        return await self.get(f"/api/v1/graph/node/{node_id}", repo_identifier=repo)


class Agent(Base):
    """A person's coding agent, holding that person's key."""

    def __init__(self, http: httpx.AsyncClient, calls: Calls, email: str, key: str) -> None:
        super().__init__(http, calls, email)
        self.email = email
        self.key = key
        self._id = 0

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}

    async def tool(self, name: str, **arguments: Any) -> Reply:
        self._id += 1
        args = {k: v for k, v in arguments.items() if v is not None}
        resp = await self.http.post(
            API + "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            },
            headers={
                **self.headers(),
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
        self.calls.add(self.who, "MCP", name, resp.status_code)
        reply = _reply(resp)
        if isinstance(reply.body, dict) and "result" in reply.body:
            content = reply.body["result"].get("content") or []
            text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
            try:
                reply.body = json.loads(text)
            except ValueError:
                reply.body = text
        return reply

    async def search(self, query: str, repo: str | None = None, limit: int | None = None) -> Reply:
        return await self.tool(
            "search_conceptual_context", query=query, repo_identifier=repo, limit=limit
        )

    async def active_decisions(self, repo: str | None = None, limit: int | None = None) -> Reply:
        return await self.tool("view_active_decisions", repo_identifier=repo, limit=limit)

    async def dependency_flow(self, path: str) -> Reply:
        return await self.tool("get_dependency_flow", file_path=path)


class Admin(Base):
    """An admin API key."""

    def __init__(self, http: httpx.AsyncClient, calls: Calls, key: str, who: str = "admin-key"):
        super().__init__(http, calls, who)
        self.key = key

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}

    async def records(self, **params: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = None
        while True:
            r = await self.get("/api/v1/admin/records", limit=100, cursor=cursor, **params)
            if not r.ok:
                raise RuntimeError(f"records list {r.status}: {r.text[:300]}")
            out.extend(r.body.get("items", []))
            cursor = r.body.get("next_cursor")
            if not cursor:
                return out

    async def record(self, record_id: str) -> Reply:
        return await self.get(f"/api/v1/admin/records/{record_id}")

    async def audit(self, since: str | None = None, **filters: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = None
        while True:
            r = await self.get(
                "/api/v1/admin/audit", limit=500, cursor=cursor, since=since, **filters
            )
            if not r.ok:
                raise RuntimeError(f"audit {r.status}: {r.text[:300]}")
            out.extend(r.body.get("items", []))
            cursor = r.body.get("next_cursor")
            if not cursor:
                return out

    async def usage(self, days: int = 2) -> dict[str, Any]:
        r = await self.get("/api/v1/admin/usage", days=days)
        return r.body if r.ok else {"error": r.status, "body": r.body}

    async def stats(self, days: int = 2) -> dict[str, Any]:
        r = await self.get("/api/v1/admin/stats", days=days)
        return r.body if r.ok else {"error": r.status, "body": r.body}

    async def scim(self, method: str, path: str, body: Any = None) -> Reply:
        resp = await self.http.request(
            method,
            API + "/scim/v2" + path,
            json=body,
            headers={**self.headers(), "Content-Type": "application/scim+json"},
        )
        self.calls.add("scim", method, "/scim/v2" + path, resp.status_code)
        return _reply(resp)


def scim_user(person: Any, style: str) -> dict[str, Any]:
    given, _, family = person.name.partition(" ")
    if style == "entra":
        return {
            "schemas": [SCIM_USER, SCIM_ENTERPRISE],
            "externalId": f"entra-{person.key}",
            "userName": person.email,
            "active": True,
            "displayName": person.name,
            "emails": [{"primary": True, "type": "work", "value": person.email}],
            "name": {"formatted": person.name, "familyName": family, "givenName": given},
            SCIM_ENTERPRISE: {"department": person.role},
        }
    return {
        "schemas": [SCIM_USER],
        "userName": person.email,
        "externalId": f"00u{person.key}",
        "name": {"givenName": given, "familyName": family},
        "emails": [{"primary": True, "value": person.email, "type": "work"}],
        "displayName": person.name,
        "locale": "en-US",
        "active": True,
    }


ENTRA_DEACTIVATE = {
    "schemas": [SCIM_PATCH],
    "Operations": [{"op": "Replace", "path": "active", "value": "False"}],
}


def to_host(url: str, world_port: int) -> str:
    """The browser runs on the host, where the world's in-container name is localhost."""
    parts = urlsplit(url)
    if parts.hostname == "host.docker.internal":
        return urlunsplit(parts._replace(netloc=f"localhost:{world_port}"))
    return url


class Console(Base):
    """A browser session: the sign-in cookie, and the CSRF header on writes."""

    def __init__(self, calls: Calls, email: str, world_port: int) -> None:
        super().__init__(httpx.AsyncClient(timeout=60, follow_redirects=False), calls, email)
        self.email = email
        self.world_port = world_port
        self.csrf: tuple[str, str] | None = None
        self.landing: str | None = None

    def headers(self) -> dict[str, str]:
        return {self.csrf[0]: self.csrf[1]} if self.csrf else {}

    async def close(self) -> None:
        await self.http.aclose()

    async def sign_in(self, login_hint: str | None = None) -> str:
        """Run the browser flow; return where the browser lands."""
        r = await self.http.get(API + "/api/v1/auth/login", params={"return_to": WEB + "/"})
        self.calls.add(self.who, "GET", "/api/v1/auth/login", r.status_code)
        target = r.headers.get("location", "")
        for _ in range(6):
            if not target or target.startswith(WEB):
                break
            url = to_host(target, self.world_port)
            if "/idp/authorize" in url:
                url += "&" + urlencode({"login_hint": login_hint or self.email})
            r = await self.http.get(url)
            self.calls.add(self.who, "GET", urlsplit(url).path, r.status_code)
            target = r.headers.get("location", "")
        self.landing = target
        me = await self.call("GET", "/api/v1/auth/me")
        if me.ok and me.body.get("csrf_header") and me.body.get("csrf_token"):
            self.csrf = (me.body["csrf_header"], me.body["csrf_token"])
        return str(target)

    def cookie(self) -> str | None:
        return self.http.cookies.get("checkcord_session")

    async def without_csrf(self, method: str, path: str, body: Any = None) -> Reply:
        resp = await self.http.request(method, API + path, json=body)
        self.calls.add(self.who + " (no csrf)", method, path, resp.status_code)
        return _reply(resp)


@dataclass
class Cli:
    code: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return self.out + ("\n" + self.err if self.err else "")


def compose(*args: str, input_text: str | None = None, timeout: float = 600) -> Cli:
    proc = subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return Cli(proc.returncode, proc.stdout, proc.stderr)


async def cli(*args: str, env: dict[str, str] | None = None, timeout: float = 600) -> Cli:
    flags: list[str] = []
    for k, v in (env or {}).items():
        flags += ["-e", f"{k}={v}"]
    return await asyncio.to_thread(
        compose, "exec", "-T", *flags, "api", "checkcord", *args, timeout=timeout
    )


async def sql(query: str) -> list[list[str]]:
    res = await asyncio.to_thread(
        compose,
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "checkcord",
        "-d",
        "checkcord",
        "-At",
        "-F",
        "\t",
        "-c",
        query,
    )
    if res.code != 0:
        raise RuntimeError(f"psql failed: {res.err.strip()[:300]}")
    return [line.split("\t") for line in res.out.splitlines() if line]


async def worker_metrics() -> str:
    res = await asyncio.to_thread(
        compose,
        "exec",
        "-T",
        "worker",
        "python",
        "-c",
        "import urllib.request,sys;"
        "sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:9100/metrics').read().decode())",
        timeout=60,
    )
    if res.code != 0:
        raise RuntimeError(f"worker metrics: {res.err.strip()[:300]}")
    return res.out


class WorldClient:
    def __init__(self, port: int) -> None:
        self.base = f"http://localhost:{port}"
        self.http = httpx.AsyncClient(timeout=120)
        self.sent: list[dict[str, Any]] = []

    async def close(self) -> None:
        await self.http.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        r = await self.http.post(self.base + "/control" + path, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"world {path} {r.status_code}: {r.text[:300]}")
        self.sent.append({"at": round(time.time(), 3), "path": path, "body": body})
        return r.json()

    async def get(self, route: str, /, **params: Any) -> Any:
        r = await self.http.get(self.base + "/control" + route, params=params)
        r.raise_for_status()
        return r.json()

    async def post(
        self, channel: str, person: str | None, text: str, **extra: Any
    ) -> dict[str, Any]:
        result: dict[str, Any] = await self._post(
            "/slack/post", {"channel": channel, "person": person, "text": text, **extra}
        )
        return result

    async def call(self, path: str, **body: Any) -> Any:
        return await self._post(path, body)

    async def requests(self, since: int = 0, service: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"since": since}
        if service:
            params["service"] = service
        result: dict[str, Any] = await self.get("/requests", **params)
        return result
