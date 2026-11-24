"""Zoom stand-in: signed event notifications and the transcript download they point to."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Any

import aiohttp
from aiohttp import web

from sim.world.state import Deliveries


def _clock(seconds: float) -> str:
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.000"


def vtt(lines: list[tuple[str, str]], minutes: int) -> str:
    step = max(1.0, minutes * 60 / max(len(lines), 1))
    cues = ["WEBVTT", ""]
    for i, (speaker, text) in enumerate(lines):
        start = i * step
        cues += [str(i + 1), f"{_clock(start)} --> {_clock(start + step - 0.5)}"]
        cues += [f"{speaker}: {text}", ""]
    return "\n".join(cues)


class Zoom:
    def __init__(self, public_base: str, deliveries: Deliveries) -> None:
        self.public_base = public_base
        self.deliveries = deliveries
        self.webhook_url: str | None = None
        self.secret: str | None = None
        self.account_id = "zoom-acct-marrowfield"
        self.files: dict[str, dict[str, str]] = {}
        self.downloads: list[dict[str, Any]] = []
        self._seq = 80_000_000

    def routes(self, app: web.Application) -> None:
        app.router.add_get("/zoom/rec/download/{file_id}", self.download)

    async def download(self, request: web.Request) -> web.StreamResponse:
        f = self.files.get(request.match_info["file_id"])
        auth = request.headers.get("Authorization", "")
        given = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        ok = f is not None and bool(given) and hmac.compare_digest(given, f["token"])
        self.downloads.append(
            {"file": request.match_info["file_id"], "token_given": bool(given), "ok": ok}
        )
        if f is None:
            return web.json_response({"code": 3301, "message": "File not found."}, status=404)
        if not ok:
            return web.json_response({"code": 124, "message": "Invalid access token."}, status=401)
        return web.Response(text=f["vtt"], content_type="text/vtt")

    def _headers(self, body: bytes, at: float | None = None) -> dict[str, str]:
        ts = str(int(at if at is not None else time.time()))
        mac = hmac.new(
            (self.secret or "").encode(), f"v0:{ts}:{body.decode()}".encode(), hashlib.sha256
        ).hexdigest()
        return {
            "Content-Type": "application/json",
            "x-zm-request-timestamp": ts,
            "x-zm-signature": f"v0={mac}",
        }

    async def _send(self, payload: dict[str, Any], kind: str) -> dict[str, Any]:
        body = json.dumps(payload).encode()
        status, text = 0, ""
        if self.webhook_url:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        self.webhook_url, data=body, headers=self._headers(body)
                    ) as resp:
                        status, text = resp.status, await resp.text()
            except aiohttp.ClientError as err:
                text = str(err)
        return self.deliveries.add(service="zoom", event=kind, status=status, response=text[:1000])

    async def validate(self) -> dict[str, Any]:
        plain = secrets.token_urlsafe(16)
        result = await self._send(
            {
                "event": "endpoint.url_validation",
                "event_ts": int(time.time() * 1000),
                "payload": {"plainToken": plain},
            },
            "endpoint.url_validation",
        )
        expected = hmac.new(
            (self.secret or "").encode(), plain.encode(), hashlib.sha256
        ).hexdigest()
        try:
            answer = json.loads(result.get("response") or "{}")
        except ValueError:
            answer = {}
        result["valid"] = (
            answer.get("plainToken") == plain and answer.get("encryptedToken") == expected
        )
        return result

    async def meeting(
        self,
        topic: str,
        host_email: str,
        lines: list[tuple[str, str]],
        minutes: int,
        start: float | None = None,
    ) -> dict[str, Any]:
        self._seq += 1
        meeting_id = self._seq
        uuid = secrets.token_urlsafe(16)
        file_id = secrets.token_hex(12)
        token = secrets.token_urlsafe(32)
        self.files[file_id] = {"vtt": vtt(lines, minutes), "token": token}
        begin = start or time.time() - minutes * 60
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(begin))
        ended = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(begin + minutes * 60))
        payload = {
            "event": "recording.transcript_completed",
            "event_ts": int(time.time() * 1000),
            "payload": {
                "account_id": self.account_id,
                "object": {
                    "uuid": uuid,
                    "id": meeting_id,
                    "account_id": self.account_id,
                    "host_id": "zhost" + secrets.token_hex(4),
                    "topic": topic,
                    "type": 2,
                    "start_time": started,
                    "timezone": "UTC",
                    "host_email": host_email,
                    "duration": minutes,
                    "total_size": len(self.files[file_id]["vtt"]),
                    "recording_count": 1,
                    "share_url": f"{self.public_base}/zoom/rec/share/{uuid}",
                    "recording_files": [
                        {
                            "id": file_id,
                            "meeting_id": uuid,
                            "recording_start": started,
                            "recording_end": ended,
                            "file_type": "TRANSCRIPT",
                            "file_extension": "VTT",
                            "file_size": len(self.files[file_id]["vtt"]),
                            "download_url": f"{self.public_base}/zoom/rec/download/{file_id}",
                            "status": "completed",
                            "recording_type": "audio_transcript",
                        }
                    ],
                },
            },
            "download_token": token,
        }
        result = await self._send(payload, "recording.transcript_completed")
        result["meeting_id"] = meeting_id
        result["file_id"] = file_id
        result["downloads"] = [d for d in self.downloads if d["file"] == file_id]
        return result
