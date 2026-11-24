"""Notion stand-in: OAuth install, page search and block children."""

from __future__ import annotations

import base64
import secrets
import time
import uuid
from typing import Any

from aiohttp import web

SETTLED_AGO = 300


def _iso(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(at))


def _text(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "plain_text": text, "text": {"content": text}}]


class Notion:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.workspace_id = str(uuid.uuid4())
        self.codes: dict[str, str] = {}
        self.tokens: set[str] = set()
        self.pages: dict[str, dict[str, Any]] = {}
        self.blocks: dict[str, list[dict[str, Any]]] = {}

    def routes(self, app: web.Application) -> None:
        r = app.router
        r.add_get("/notion/v1/oauth/authorize", self.authorize)
        r.add_post("/notion/v1/oauth/token", self.token)
        r.add_post("/notion/v1/search", self.search)
        r.add_get("/notion/v1/blocks/{id}/children", self.children)

    def _ok(self, request: web.Request) -> bool:
        return request.headers.get("Authorization", "").removeprefix("Bearer ") in self.tokens

    async def authorize(self, request: web.Request) -> web.StreamResponse:
        q = request.query
        if q.get("client_id") != self.client_id:
            return web.json_response({"error": "invalid_client"}, status=400)
        code = secrets.token_urlsafe(16)
        self.codes[code] = q.get("redirect_uri", "")
        sep = "&" if "?" in self.codes[code] else "?"
        raise web.HTTPFound(f"{self.codes[code]}{sep}code={code}&state={q.get('state', '')}")

    async def token(self, request: web.Request) -> web.Response:
        given = request.headers.get("Authorization", "").removeprefix("Basic ")
        want = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        b = await request.json()
        if given != want or b.get("code") not in self.codes:
            return web.json_response({"error": "invalid_grant"}, status=400)
        self.codes.pop(b["code"])
        access = "secret_" + secrets.token_urlsafe(24)
        self.tokens.add(access)
        return web.json_response(
            {
                "access_token": access,
                "token_type": "bearer",
                "bot_id": str(uuid.uuid4()),
                "workspace_id": self.workspace_id,
                "workspace_name": "Marrowfield",
            }
        )

    def add_page(
        self, title: str, paragraphs: list[str], parent: str, by: str, at: float | None = None
    ) -> dict[str, Any]:
        pid = str(uuid.uuid4())
        edited = at if at is not None else time.time() - SETTLED_AGO
        page = {
            "object": "page",
            "id": pid,
            "created_time": _iso(edited - 60),
            "last_edited_time": _iso(edited),
            "created_by": {"object": "user", "id": by},
            "last_edited_by": {"object": "user", "id": by},
            "parent": {"type": "page_id", "page_id": parent},
            "archived": False,
            "url": f"https://www.notion.so/{pid.replace('-', '')}",
            "properties": {"title": {"id": "title", "type": "title", "title": _text(title)}},
        }
        self.pages[pid] = page
        self.blocks[pid] = [
            {
                "object": "block",
                "id": str(uuid.uuid4()),
                "type": "paragraph",
                "has_children": False,
                "paragraph": {"rich_text": _text(p)},
            }
            for p in paragraphs
        ]
        return page

    async def search(self, request: web.Request) -> web.Response:
        if not self._ok(request):
            return web.json_response({"object": "error", "code": "unauthorized"}, status=401)
        b = await request.json()
        rows = sorted(self.pages.values(), key=lambda p: (p["last_edited_time"], p["id"]))
        start = int(b.get("start_cursor") or 0)
        size = min(int(b.get("page_size", 100)), 100)
        chunk = rows[start : start + size]
        more = start + size < len(rows)
        return web.json_response(
            {
                "object": "list",
                "results": chunk,
                "has_more": more,
                "next_cursor": str(start + size) if more else None,
            }
        )

    async def children(self, request: web.Request) -> web.Response:
        if not self._ok(request):
            return web.json_response({"object": "error", "code": "unauthorized"}, status=401)
        blocks = self.blocks.get(request.match_info["id"])
        if blocks is None:
            return web.json_response({"object": "error", "code": "object_not_found"}, status=404)
        return web.json_response(
            {"object": "list", "results": blocks, "has_more": False, "next_cursor": None}
        )
