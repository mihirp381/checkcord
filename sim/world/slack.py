"""Slack stand-in: OAuth, the Web API calls Checkcord makes, and Socket Mode delivery."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
import uuid
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qsl, urlencode

from aiohttp import WSMsgType, web

from sim import company
from sim.world.state import SlackChannel, SlackMessage, SlackUser

APP_ID = "A0CHECKCRD"
BOT_USER = "U0BOT00001"
BOT_ID = "B0CHECKCRD"
ACK_SECONDS = 3.0
MAX_RETRIES = 3
PAGE = 2


def _ts(at: float | None = None) -> str:
    return f"{(at if at is not None else time.time()):.6f}"


def _page(items: list[Any], cursor: str | None, limit: int | None) -> tuple[list[Any], str]:
    start = int(base64.b64decode(cursor).decode()) if cursor else 0
    size = max(1, min(limit or PAGE, PAGE))
    chunk = items[start : start + size]
    nxt = start + size
    return chunk, base64.b64encode(str(nxt).encode()).decode() if nxt < len(items) else ""


class Slack:
    def __init__(self, public_base: str, client_id: str, client_secret: str, app_token: str):
        self.public_base = public_base.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.app_token = app_token
        self.bot_tokens: set[str] = set()
        self.codes: dict[str, str] = {}
        self.installed = False
        self.users: dict[str, SlackUser] = {}
        self.channels: dict[str, SlackChannel] = {}
        self.messages: dict[str, list[SlackMessage]] = {}
        self.files: dict[str, str] = {}
        self.sockets: list[web.WebSocketResponse] = []
        self.unacked: dict[str, dict[str, Any]] = {}
        self.envelopes: list[dict[str, Any]] = []
        self.acks: list[dict[str, Any]] = []
        self.queue: list[dict[str, Any]] = []
        self.rate_limit_next = 0
        self.rate_limited = 0
        self.uninstalled = 0
        self._event_seq = 0
        self._rr = 0
        self._last_ts = 0.0
        for p in company.PEOPLE:
            self.users[p.slack_id] = SlackUser(p.slack_id, p.key, p.name, p.profile_email)
        for c in company.CHANNELS:
            self.channels[c.id] = SlackChannel(
                c.id, c.name, c.private, [company.person(k).slack_id for k in c.members]
            )
            self.messages[c.id] = []

    def routes(self, app: web.Application) -> None:
        app.router.add_get("/oauth/v2/authorize", self.authorize)
        app.router.add_get("/slack/socket", self.socket)
        app.router.add_get("/slack/files/{id}/{name}", self.file)
        app.router.add_route("*", "/api/{method}", self.web_api)

    def next_ts(self, at: float | None = None) -> str:
        value = at if at is not None else time.time()
        if value <= self._last_ts:
            value = self._last_ts + 0.0001
        self._last_ts = value
        return _ts(value)

    async def authorize(self, request: web.Request) -> web.StreamResponse:
        q = request.query
        if q.get("client_id") != self.client_id:
            return web.Response(status=400, text="invalid client_id")
        code = secrets.token_hex(12)
        self.codes[code] = q.get("redirect_uri", "")
        target = q.get("redirect_uri", "") + "?" + urlencode({"code": code, "state": q["state"]})
        raise web.HTTPFound(target)

    async def file(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Authorization", "").removeprefix("Bearer ") not in self.bot_tokens:
            return web.Response(status=403)
        text = self.files.get(request.match_info["id"])
        if text is None:
            return web.Response(status=404)
        return web.Response(text=text, content_type="text/plain")

    async def _params(self, request: web.Request) -> dict[str, Any]:
        params: dict[str, Any] = dict(request.query)
        if request.body_exists:
            raw = await request.read()
            ctype = request.content_type
            if ctype == "application/json" and raw:
                params.update(json.loads(raw))
            elif raw:
                params.update(dict(parse_qsl(raw.decode())))
        return params

    async def web_api(self, request: web.Request) -> web.StreamResponse:
        method = request.match_info["method"]
        params = await self._params(request)
        if self.rate_limit_next > 0:
            self.rate_limit_next -= 1
            self.rate_limited += 1
            return web.json_response(
                {"ok": False, "error": "ratelimited"}, status=429, headers={"Retry-After": "1"}
            )
        auth = request.headers.get("Authorization", "")
        if method == "oauth.v2.access":
            return web.json_response(self._oauth_access(auth, params))
        token = auth.removeprefix("Bearer ") or str(params.get("token", ""))
        if method == "apps.connections.open":
            if token != self.app_token:
                return web.json_response({"ok": False, "error": "invalid_auth"})
            url = self.public_base.replace("http", "ws", 1) + "/slack/socket"
            return web.json_response({"ok": True, "url": f"{url}?ticket={uuid.uuid4()}"})
        if method == "apps.uninstall":
            self.uninstalled += 1
            self.installed = False
            self.bot_tokens.clear()
            return web.json_response({"ok": True})
        if token not in self.bot_tokens:
            return web.json_response({"ok": False, "error": "invalid_auth"})
        handler = getattr(self, "_" + method.replace(".", "_"), None)
        if handler is None:
            return web.json_response({"ok": False, "error": "unknown_method"})
        body: dict[str, Any] = handler(params)
        return web.json_response(body)

    def _oauth_access(self, auth: str, params: dict[str, Any]) -> dict[str, Any]:
        client_id, client_secret = params.get("client_id"), params.get("client_secret")
        if auth.startswith("Basic "):
            client_id, _, client_secret = base64.b64decode(auth[6:]).decode().partition(":")
        if client_id != self.client_id or client_secret != self.client_secret:
            return {"ok": False, "error": "invalid_client_id"}
        if self.codes.pop(str(params.get("code")), None) is None:
            return {"ok": False, "error": "invalid_code"}
        token = "xoxb-" + secrets.token_hex(16)
        self.bot_tokens.add(token)
        self.installed = True
        return {
            "ok": True,
            "app_id": APP_ID,
            "authed_user": {"id": company.person("priya").slack_id},
            "scope": ",".join(company_scopes()),
            "token_type": "bot",
            "access_token": token,
            "bot_user_id": BOT_USER,
            "team": {"id": company.TEAM_ID, "name": company.TEAM_NAME},
            "enterprise": None,
            "is_enterprise_install": False,
        }

    def _auth_test(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "url": "https://marrowfield.slack.example/",
            "team": company.TEAM_NAME,
            "user": "checkcord",
            "team_id": company.TEAM_ID,
            "user_id": BOT_USER,
            "bot_id": BOT_ID,
            "is_enterprise_install": False,
        }

    def _team_info(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "team": {"id": company.TEAM_ID, "name": company.TEAM_NAME, "domain": "marrowfield"},
        }

    def _channel_wire(self, ch: SlackChannel) -> dict[str, Any]:
        return {
            "id": ch.id,
            "name": ch.name,
            "is_channel": True,
            "is_group": False,
            "is_private": ch.private,
            "is_archived": False,
            "is_member": True,
            "created": ch.created,
            "num_members": len(ch.members),
        }

    def _conversations_info(self, params: dict[str, Any]) -> dict[str, Any]:
        ch = self.channels.get(str(params.get("channel")))
        if ch is None:
            return {"ok": False, "error": "channel_not_found"}
        return {"ok": True, "channel": self._channel_wire(ch)}

    def _conversations_members(self, params: dict[str, Any]) -> dict[str, Any]:
        ch = self.channels.get(str(params.get("channel")))
        if ch is None:
            return {"ok": False, "error": "channel_not_found"}
        chunk, nxt = _page(list(ch.members), params.get("cursor"), _int(params.get("limit")))
        return {"ok": True, "members": chunk, "response_metadata": {"next_cursor": nxt}}

    def _conversations_list(self, params: dict[str, Any]) -> dict[str, Any]:
        types = str(params.get("types") or "public_channel")
        rows = [
            self._channel_wire(c)
            for c in self.channels.values()
            if ("private_channel" in types and c.private)
            or ("public_channel" in types and not c.private)
        ]
        chunk, nxt = _page(rows, params.get("cursor"), _int(params.get("limit")))
        return {"ok": True, "channels": chunk, "response_metadata": {"next_cursor": nxt}}

    def _visible(self, channel: str) -> list[SlackMessage]:
        return [m for m in self.messages.get(channel, []) if not m.deleted]

    def _conversations_history(self, params: dict[str, Any]) -> dict[str, Any]:
        channel = str(params.get("channel"))
        if channel not in self.channels:
            return {"ok": False, "error": "channel_not_found"}
        oldest = float(params.get("oldest") or 0)
        latest = float(params.get("latest") or 1e12)
        rows = [
            m
            for m in self._visible(channel)
            if (m.thread_ts is None or m.thread_ts == m.ts) and oldest < float(m.ts) <= latest
        ]
        rows.sort(key=lambda m: float(m.ts), reverse=True)
        chunk, nxt = _page(
            [m.wire() for m in rows], params.get("cursor"), _int(params.get("limit"))
        )
        return {
            "ok": True,
            "messages": chunk,
            "has_more": bool(nxt),
            "response_metadata": {"next_cursor": nxt},
        }

    def _conversations_replies(self, params: dict[str, Any]) -> dict[str, Any]:
        channel, ts = str(params.get("channel")), str(params.get("ts"))
        oldest = float(params.get("oldest") or 0)
        rows = [
            m
            for m in self._visible(channel)
            if (m.ts == ts or m.thread_ts == ts) and (m.ts == ts or float(m.ts) > oldest)
        ]
        rows.sort(key=lambda m: float(m.ts))
        if not rows:
            return {"ok": False, "error": "thread_not_found"}
        chunk, nxt = _page(
            [m.wire() for m in rows], params.get("cursor"), _int(params.get("limit"))
        )
        return {
            "ok": True,
            "messages": chunk,
            "has_more": bool(nxt),
            "response_metadata": {"next_cursor": nxt},
        }

    def _user_wire(self, u: SlackUser) -> dict[str, Any]:
        return {
            "id": u.id,
            "team_id": company.TEAM_ID,
            "name": u.name,
            "real_name": u.real_name,
            "deleted": u.deleted,
            "is_bot": u.is_bot,
            "profile": {
                "real_name": u.real_name,
                "display_name": u.name,
                "email": u.email,
            },
        }

    def _users_list(self, params: dict[str, Any]) -> dict[str, Any]:
        rows = [self._user_wire(u) for u in self.users.values()]
        chunk, nxt = _page(rows, params.get("cursor"), _int(params.get("limit")))
        return {"ok": True, "members": chunk, "response_metadata": {"next_cursor": nxt}}

    def _users_info(self, params: dict[str, Any]) -> dict[str, Any]:
        u = self.users.get(str(params.get("user")))
        if u is None:
            return {"ok": False, "error": "user_not_found"}
        return {"ok": True, "user": self._user_wire(u)}

    def _usergroups_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "usergroups": []}

    async def socket(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(autoping=True)
        await ws.prepare(request)
        self.sockets.append(ws)
        await ws.send_json(
            {
                "type": "hello",
                "num_connections": len(self.sockets),
                "debug_info": {"host": "sim-slack", "approximate_connection_time": 18060},
                "connection_info": {"app_id": APP_ID},
            }
        )
        pending, self.queue = self.queue, []
        for envelope in pending:
            await self._send(envelope)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    envelope_id = data.get("envelope_id")
                    if envelope_id:
                        self.acks.append({"envelope_id": envelope_id, "at": time.time()})
                        self.unacked.pop(envelope_id, None)
        finally:
            if ws in self.sockets:
                self.sockets.remove(ws)
        return ws

    @property
    def connected(self) -> bool:
        return bool(self.sockets)

    async def _send(self, envelope: dict[str, Any]) -> None:
        live = [s for s in self.sockets if not s.closed]
        if not live:
            self.queue.append(envelope)
            return
        ws = live[self._rr % len(live)]
        self._rr += 1
        self.unacked[envelope["envelope_id"]] = envelope
        await ws.send_json(envelope)
        asyncio.get_running_loop().call_later(
            ACK_SECONDS, lambda: asyncio.ensure_future(self._redeliver(envelope["envelope_id"]))
        )

    async def _redeliver(self, envelope_id: str) -> None:
        envelope = self.unacked.pop(envelope_id, None)
        if envelope is None or envelope["retry_attempt"] >= MAX_RETRIES:
            return
        again = {
            **envelope,
            "envelope_id": str(uuid.uuid4()),
            "retry_attempt": envelope["retry_attempt"] + 1,
            "retry_reason": "timeout",
        }
        self.envelopes.append(again)
        await self._send(again)

    async def deliver(self, event: dict[str, Any]) -> dict[str, Any]:
        self._event_seq += 1
        event.setdefault("event_ts", _ts())
        envelope = {
            "envelope_id": str(uuid.uuid4()),
            "type": "events_api",
            "accepts_response_payload": False,
            "retry_attempt": 0,
            "retry_reason": "",
            "payload": {
                "token": "verification-unused",
                "team_id": company.TEAM_ID,
                "api_app_id": APP_ID,
                "event": event,
                "type": "event_callback",
                "event_id": f"Ev{self._event_seq:08d}",
                "event_time": int(time.time()),
                "authorizations": [
                    {
                        "enterprise_id": None,
                        "team_id": company.TEAM_ID,
                        "user_id": BOT_USER,
                        "is_bot": True,
                        "is_enterprise_install": False,
                    }
                ],
                "is_ext_shared_channel": False,
            },
        }
        self.envelopes.append(envelope)
        await self._send(envelope)
        return envelope

    def find(self, channel: str, ts: str) -> SlackMessage | None:
        for m in self.messages.get(channel, []):
            if m.ts == ts:
                return m
        return None

    async def post(
        self,
        channel: str,
        user: str | None,
        text: str,
        *,
        at: float | None = None,
        thread_ts: str | None = None,
        subtype: str | None = None,
        bot_id: str | None = None,
        username: str | None = None,
        file_text: str | None = None,
        file_name: str = "notes.txt",
        deliver: bool = True,
    ) -> SlackMessage:
        ch = self.channels[channel]
        ts = self.next_ts(at)
        files: list[dict[str, Any]] = []
        if file_text is not None:
            fid = "F" + secrets.token_hex(5).upper()
            self.files[fid] = file_text
            files.append(
                {
                    "id": fid,
                    "name": file_name,
                    "mimetype": "text/plain",
                    "size": len(file_text.encode()),
                    "url_private": f"{self.public_base}/slack/files/{fid}/{file_name}",
                }
            )
        msg = SlackMessage(
            channel, ts, user, text, thread_ts, subtype, bot_id, username, files=files
        )
        if file_text is not None and subtype is None:
            msg.subtype = "file_share"
        if thread_ts:
            parent = self.find(channel, thread_ts)
            if parent is not None:
                parent.reply_count += 1
                parent.thread_ts = parent.ts
        self.messages[channel].append(msg)
        if deliver:
            event = {
                **msg.wire(),
                "channel": channel,
                "channel_type": "group" if ch.private else "channel",
                "team": company.TEAM_ID,
                "event_ts": ts,
            }
            if msg.subtype == "file_share":
                event["upload"] = False
            await self.deliver(event)
        return msg

    async def send_existing(self, channel: str, ts: str) -> None:
        msg = self.find(channel, ts)
        if msg is None:
            raise KeyError(ts)
        await self.deliver(
            {
                **msg.wire(),
                "channel": channel,
                "channel_type": "group" if self.channels[channel].private else "channel",
                "team": company.TEAM_ID,
                "event_ts": ts,
            }
        )

    async def redeliver_event(self, ts: str) -> dict[str, Any]:
        sent = [e for e in self.envelopes if e["payload"]["event"].get("ts") == ts]
        if not sent:
            raise KeyError(ts)
        first = sent[0]
        again = {
            **first,
            "envelope_id": str(uuid.uuid4()),
            "retry_attempt": first["retry_attempt"] + 1,
            "retry_reason": "timeout",
        }
        self.envelopes.append(again)
        await self._send(again)
        return {"event_id": again["payload"]["event_id"], "retry_attempt": again["retry_attempt"]}

    async def edit(self, channel: str, ts: str, text: str, at: float | None = None) -> None:
        msg = self.find(channel, ts)
        if msg is None:
            raise KeyError(ts)
        previous = msg.wire()
        msg.text = text
        msg.edited_ts = self.next_ts(at)
        await self.deliver(
            {
                "type": "message",
                "subtype": "message_changed",
                "hidden": True,
                "channel": channel,
                "team": company.TEAM_ID,
                "ts": msg.edited_ts,
                "event_ts": msg.edited_ts,
                "message": {**msg.wire(), "team": company.TEAM_ID},
                "previous_message": previous,
                "channel_type": "group" if self.channels[channel].private else "channel",
            }
        )

    async def delete(self, channel: str, ts: str, at: float | None = None) -> None:
        msg = self.find(channel, ts)
        if msg is None:
            raise KeyError(ts)
        previous = msg.wire()
        msg.deleted = True
        now = self.next_ts(at)
        await self.deliver(
            {
                "type": "message",
                "subtype": "message_deleted",
                "hidden": True,
                "channel": channel,
                "team": company.TEAM_ID,
                "ts": now,
                "event_ts": now,
                "deleted_ts": ts,
                "previous_message": previous,
                "channel_type": "group" if self.channels[channel].private else "channel",
            }
        )

    async def membership(self, channel: str, user: str, joined: bool) -> None:
        ch = self.channels[channel]
        if joined and user not in ch.members:
            ch.members.append(user)
        if not joined and user in ch.members:
            ch.members.remove(user)
        event: dict[str, Any] = {
            "type": "member_joined_channel" if joined else "member_left_channel",
            "user": user,
            "channel": channel,
            "channel_type": "G" if ch.private else "C",
            "team": company.TEAM_ID,
        }
        if joined:
            event["inviter"] = company.person("ingrid").slack_id
            await self.post(
                channel, user, f"<@{user}> has joined the channel", subtype="channel_join"
            )
        await self.deliver(event)

    async def create_channel(self, cid: str, name: str, private: bool, members: list[str]) -> None:
        self.channels[cid] = SlackChannel(cid, name, private, list(members))
        self.messages.setdefault(cid, [])
        kind = "group_joined" if private else "channel_created"
        await self.deliver(
            {
                "type": kind,
                "channel": {
                    "id": cid,
                    "name": name,
                    "is_private": private,
                    "created": int(time.time()),
                },
            }
        )

    async def convert_to_private(self, channel: str, by: str) -> None:
        self.channels[channel].private = True
        await self.deliver({"type": "channel_converted_to_private", "channel": channel, "user": by})

    async def uninstall(self) -> None:
        self.installed = False
        self.bot_tokens.clear()
        await self.deliver({"type": "app_uninstalled"})

    def state(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "connected_sockets": len([s for s in self.sockets if not s.closed]),
            "envelopes": len(self.envelopes),
            "acks": len(self.acks),
            "unacked": list(self.unacked),
            "redeliveries": len([e for e in self.envelopes if e["retry_attempt"] > 0]),
            "queued": len(self.queue),
            "rate_limited": self.rate_limited,
            "uninstalled": self.uninstalled,
            "channels": {
                c.name: {"id": c.id, "private": c.private, "members": list(c.members)}
                for c in self.channels.values()
            },
        }


def company_scopes() -> Iterable[str]:
    return (
        "channels:history",
        "groups:history",
        "channels:read",
        "groups:read",
        "users:read",
        "users:read.email",
        "usergroups:read",
    )


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
