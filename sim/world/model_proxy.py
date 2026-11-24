"""Model stand-in: forwards OpenAI-style calls to the local model server, or fails on demand."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import aiohttp
from aiohttp import web

from sim.world.state import ModelState

MODES = ("ok", "down", "garbage")


def valid_mode(mode: str) -> bool:
    if mode in MODES:
        return True
    head, _, seconds = mode.partition(":")
    try:
        return head == "slow" and float(seconds) >= 0
    except ValueError:
        return False


def kind_of(path: str) -> str:
    return "embeddings" if path.endswith("embeddings") else "chat"


class ModelProxy:
    def __init__(self, upstream: str) -> None:
        self.upstream = upstream.rstrip("/")
        self.state = ModelState()
        self._session: aiohttp.ClientSession | None = None

    def routes(self, app: web.Application) -> None:
        app.router.add_route("*", "/model/v1/{tail:.*}", self.forward)

    def set_mode(self, mode: str) -> None:
        if not valid_mode(mode):
            raise ValueError(f"unknown model mode {mode!r}")
        self.state.mode = mode

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def forward(self, request: web.Request) -> web.StreamResponse:
        tail = request.match_info["tail"]
        raw = await request.read()
        kind = kind_of(tail)
        try:
            parsed: Any = json.loads(raw) if raw else None
        except ValueError:
            parsed = raw.decode("utf-8", "replace")
        mode = self.state.mode
        self.state.calls[kind] = self.state.calls.get(kind, 0) + 1
        self.state.bodies.append({"at": time.time(), "kind": kind, "mode": mode, "body": parsed})
        if mode == "down":
            self.state.failures[kind] = self.state.failures.get(kind, 0) + 1
            return web.json_response(
                {"error": {"message": "model server unavailable", "type": "server_error"}},
                status=500,
            )
        if mode == "garbage":
            self.state.failures[kind] = self.state.failures.get(kind, 0) + 1
            return web.Response(text='{"choices": [{"message": ', content_type="application/json")
        if mode.startswith("slow:"):
            await asyncio.sleep(float(mode.split(":", 1)[1]))
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600))
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() in ("content-type", "authorization", "accept")
        }
        url = f"{self.upstream}/{tail}"
        if request.query_string:
            url += "?" + request.query_string
        try:
            async with self._session.request(
                request.method, url, data=raw, headers=headers
            ) as resp:
                body = await resp.read()
                if resp.status >= 400:
                    self.state.failures[kind] = self.state.failures.get(kind, 0) + 1
                return web.Response(
                    body=body,
                    status=resp.status,
                    content_type=resp.content_type,
                )
        except aiohttp.ClientError as err:
            self.state.failures[kind] = self.state.failures.get(kind, 0) + 1
            return web.json_response({"error": {"message": str(err)}}, status=502)

    def summary(self) -> dict[str, Any]:
        return {
            "mode": self.state.mode,
            "calls": self.state.calls,
            "failures": self.state.failures,
            "bodies": len(self.state.bodies),
        }
