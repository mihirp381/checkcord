"""Microsoft Teams stand-in: Bot Framework token signing, Entra sign-in and the Graph calls
the Teams and Teams-meetings connectors make."""

from __future__ import annotations

import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import aiohttp
import jwt
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric import rsa

from sim import company
from sim.world.state import Deliveries

PREFIX = "/teams"
BOT_ISSUER = "https://api.botframework.com"
SERVICE_URL = "https://smba.trafficmanager.net/amer/"
MS_TENANT = "6f3c1d2e-8a4b-4c5d-9e7f-0a1b2c3d4e5f"
_NS = uuid.UUID("2b1f0c4e-1d7a-4e0b-9a51-3c6d2f8e7a90")


def guid(name: str) -> str:
    return str(uuid.uuid5(_NS, name))


def thread_id(name: str) -> str:
    return f"19:{uuid.uuid5(_NS, 'thread-' + name).hex}@thread.tacv2"


@dataclass(frozen=True)
class Channel:
    key: str
    name: str
    membership: str
    members: tuple[str, ...]

    @property
    def id(self) -> str:
        return thread_id(self.key)


TEAM_NAME = "Payments"
TEAM_ID = guid("team-payments")
CHANNELS = (
    Channel("general", "General", "standard", tuple(p.key for p in company.PEOPLE)),
    Channel("payments-ops", "Payments ops", "standard", tuple(p.key for p in company.PEOPLE)),
    Channel("payments-leads", "Payments leads", "private", ("tomas", "aiko", "ingrid")),
)
CHANNEL = {c.key: c for c in CHANNELS}


def user_id(key: str) -> str:
    return guid("user-" + key)


def _user(key: str) -> dict[str, Any]:
    p = company.person(key)
    return {
        "id": user_id(key),
        "displayName": p.name,
        "mail": p.email,
        "userPrincipalName": p.email,
        "jobTitle": p.role,
    }


def _clock(seconds: float) -> str:
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.000"


def teams_vtt(lines: list[tuple[str, str]], minutes: int) -> str:
    step = max(1.0, minutes * 60 / max(len(lines), 1))
    cues = ["WEBVTT", ""]
    for i, (key, text) in enumerate(lines):
        start = i * step
        cues += [f"{_clock(start)} --> {_clock(start + step - 0.5)}"]
        cues += [f"<v {company.person(key).name}>{text}</v>", ""]
    return "\n".join(cues)


def _graph_error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": {"code": code, "message": message}}, status=status)


_PAREN = re.compile(r"\('([^']*)'\)")


class Teams:
    def __init__(
        self,
        public_base: str,
        checkcord_url: str,
        client_id: str,
        client_secret: str,
        deliveries: Deliveries,
    ) -> None:
        self.public_base = public_base
        self.checkcord_url = checkcord_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.deliveries = deliveries
        self.kid = secrets.token_hex(6)
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.tokens: set[str] = set()
        self.subscriptions: dict[str, dict[str, Any]] = {}
        self.meetings: dict[str, dict[str, Any]] = {}
        self.transcripts: dict[str, str] = {}
        self.consents: list[dict[str, Any]] = []
        self.graph_calls: list[dict[str, Any]] = []

    def routes(self, app: web.Application) -> None:
        r = app.router
        r.add_get(PREFIX + "/botframework/openid", self.openid)
        r.add_get(PREFIX + "/botframework/keys", self.keys)
        r.add_get(PREFIX + "/login/common/adminconsent", self.adminconsent)
        r.add_post(PREFIX + "/login/{tenant}/oauth2/v2.0/token", self.token)
        r.add_route("*", PREFIX + "/graph/v1.0/{path:.*}", self.graph)

    async def openid(self, _: web.Request) -> web.Response:
        return web.json_response(
            {
                "issuer": BOT_ISSUER,
                "authorization_endpoint": "https://invalid.botframework.com",
                "jwks_uri": f"{self.public_base}{PREFIX}/botframework/keys",
                "id_token_signing_alg_values_supported": ["RS256"],
                "token_endpoint_auth_methods_supported": ["private_key_jwt"],
            }
        )

    async def keys(self, _: web.Request) -> web.Response:
        jwk: dict[str, Any] = jwt.algorithms.RSAAlgorithm.to_jwk(
            self._key.public_key(), as_dict=True
        )
        return web.json_response(
            {"keys": [{**jwk, "kid": self.kid, "use": "sig", "endorsements": ["msteams"]}]}
        )

    async def adminconsent(self, request: web.Request) -> web.StreamResponse:
        q = request.query
        self.consents.append({k: q.get(k) for k in ("client_id", "redirect_uri")})
        if q.get("client_id") != self.client_id or not q.get("redirect_uri"):
            return web.Response(status=400, text="AADSTS700016: application not found")
        query = urlencode(
            {"admin_consent": "True", "tenant": MS_TENANT, "state": q.get("state", ""), "scope": ""}
        )
        raise web.HTTPFound(f"{q['redirect_uri']}?{query}")

    async def token(self, request: web.Request) -> web.Response:
        form = await request.post()
        ok = (
            request.match_info["tenant"] == MS_TENANT
            and form.get("grant_type") == "client_credentials"
            and form.get("client_id") == self.client_id
            and form.get("client_secret") == self.client_secret
        )
        if not ok:
            return web.json_response(
                {"error": "invalid_client", "error_description": "AADSTS7000215"}, status=401
            )
        value = "eyJ0eXAi." + secrets.token_urlsafe(32)
        self.tokens.add(value)
        return web.json_response(
            {"token_type": "Bearer", "expires_in": 3599, "access_token": value}
        )

    async def graph(self, request: web.Request) -> web.StreamResponse:
        path = _PAREN.sub(r"/\1", request.match_info["path"]).strip("/")
        self.graph_calls.append({"method": request.method, "path": path})
        auth = request.headers.get("Authorization", "")
        if auth.removeprefix("Bearer ").strip() not in self.tokens:
            return _graph_error(401, "InvalidAuthenticationToken", "Access token is empty.")
        parts = path.split("/")
        if request.method == "GET":
            return await self._get(request, parts)
        if parts[0] == "subscriptions":
            return await self._subscription(request, parts)
        return _graph_error(405, "MethodNotAllowed", request.method)

    async def _get(self, request: web.Request, parts: list[str]) -> web.StreamResponse:
        if parts == ["users"]:
            return web.json_response({"value": [_user(p.key) for p in company.PEOPLE]})
        if len(parts) == 2 and parts[0] == "users":
            found = [p for p in company.PEOPLE if user_id(p.key) == parts[1]]
            if not found:
                return _graph_error(404, "Request_ResourceNotFound", "User not found.")
            return web.json_response(_user(found[0].key))
        if parts == ["groups"]:
            return web.json_response(
                {
                    "value": [
                        {
                            "id": TEAM_ID,
                            "displayName": TEAM_NAME,
                            "resourceProvisioningOptions": ["Team"],
                        }
                    ]
                }
            )
        if parts[0] == "teams" and len(parts) >= 3 and parts[2] == "channels":
            if parts[1] != TEAM_ID:
                return _graph_error(404, "NotFound", "No team found with Group Id " + parts[1])
            if len(parts) == 3:
                return web.json_response({"value": [self._channel(c) for c in CHANNELS]})
            channel = next((c for c in CHANNELS if c.id == parts[3]), None)
            if channel is None:
                return _graph_error(404, "NotFound", "Channel not found.")
            if len(parts) == 4:
                return web.json_response(self._channel(channel))
            if parts[4:] == ["members"]:
                return web.json_response({"value": [self._member(k) for k in channel.members]})
        if len(parts) >= 4 and parts[0] == "users" and parts[2] == "onlineMeetings":
            meeting = self.meetings.get(parts[3])
            if meeting is None or meeting["organizer"] != parts[1]:
                return _graph_error(404, "NotFound", "Meeting not found.")
            if len(parts) == 4:
                return web.json_response(meeting["body"])
            if len(parts) == 7 and parts[4] == "transcripts" and parts[6] == "content":
                text = self.transcripts.get(parts[5])
                if text is None:
                    return _graph_error(404, "NotFound", "Transcript not found.")
                return web.Response(text=text, content_type="text/vtt")
        return _graph_error(404, "NotFound", "Resource not found for the segment.")

    def _channel(self, c: Channel) -> dict[str, Any]:
        return {
            "id": c.id,
            "displayName": c.name,
            "description": None,
            "membershipType": c.membership,
            "webUrl": f"https://teams.microsoft.com/l/channel/{c.id}",
        }

    def _member(self, key: str) -> dict[str, Any]:
        u = _user(key)
        return {
            "@odata.type": "#microsoft.graph.aadUserConversationMember",
            "id": guid(f"member-{key}"),
            "roles": [],
            "displayName": u["displayName"],
            "userId": u["id"],
            "email": u["mail"],
            "tenantId": MS_TENANT,
        }

    async def _subscription(self, request: web.Request, parts: list[str]) -> web.StreamResponse:
        if request.method == "POST" and parts == ["subscriptions"]:
            body = await request.json()
            urls = [body.get("notificationUrl"), body.get("lifecycleNotificationUrl")]
            if not all([await self._validates(u) for u in urls if u is not None] or [False]):
                return _graph_error(
                    400, "ValidationError", "Subscription validation request failed."
                )
            sub = {**body, "id": str(uuid.uuid4()), "applicationId": self.client_id}
            self.subscriptions[sub["id"]] = sub
            return web.json_response(sub, status=201)
        if len(parts) == 2 and parts[1] in self.subscriptions:
            if request.method == "DELETE":
                self.subscriptions.pop(parts[1])
                return web.Response(status=204)
            if request.method == "PATCH":
                sub = self.subscriptions[parts[1]]
                sub.update(await request.json())
                return web.json_response(sub)
        return _graph_error(404, "ResourceNotFound", "Subscription not found.")

    async def _validates(self, url: str) -> bool:
        check = secrets.token_urlsafe(12)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    url, params={"validationToken": check}, timeout=aiohttp.ClientTimeout(10)
                ) as resp:
                    return resp.status == 200 and await resp.text() == check
        except aiohttp.ClientError:
            return False

    def _bot_token(self, stranger: bool = False) -> str:
        now = int(time.time())
        claims = {
            "iss": BOT_ISSUER,
            "aud": self.client_id,
            "serviceUrl": SERVICE_URL,
            "nbf": now - 5,
            "iat": now - 5,
            "exp": now + 3600,
        }
        key = self._stranger if stranger else self._key
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": self.kid})

    async def message(
        self,
        channel: str,
        person: str,
        text: str,
        reply_to: str | None = None,
        auth: str = "good",
    ) -> dict[str, Any]:
        c = CHANNEL[channel]
        message_id = str(int(time.time() * 1000) + secrets.randbelow(1000))
        root = reply_to or message_id
        p = company.person(person)
        activity: dict[str, Any] = {
            "type": "message",
            "id": message_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "localTimestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000+00:00", time.gmtime()),
            "serviceUrl": SERVICE_URL,
            "channelId": "msteams",
            "from": {
                "id": "29:1" + uuid.uuid5(_NS, "bf-" + person).hex,
                "name": p.name,
                "aadObjectId": user_id(person),
            },
            "conversation": {
                "isGroup": True,
                "conversationType": "channel",
                "tenantId": MS_TENANT,
                "id": f"{c.id};messageid={root}",
            },
            "recipient": {"id": "28:" + self.client_id, "name": "Checkcord"},
            "textFormat": "plain",
            "text": text,
            "attachments": [],
            "entities": [],
            "channelData": {
                "teamsChannelId": c.id,
                "teamsTeamId": CHANNEL["general"].id,
                "channel": {"id": c.id},
                "team": {"id": CHANNEL["general"].id, "aadGroupId": TEAM_ID},
                "tenant": {"id": MS_TENANT},
            },
            "locale": "en-US",
        }
        if reply_to:
            activity["replyToId"] = reply_to
        headers = {"Content-Type": "application/json"}
        if auth != "none":
            headers["Authorization"] = f"Bearer {self._bot_token(stranger=auth == 'stranger')}"
        status, answer = 0, ""
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    self.checkcord_url + "/api/messages", data=json.dumps(activity), headers=headers
                ) as resp:
                    status, answer = resp.status, await resp.text()
        except aiohttp.ClientError as err:
            answer = str(err)
        item = self.deliveries.add(
            service="teams", event="message", auth=auth, status=status, response=answer[:500]
        )
        return {**item, "id": message_id, "channel": c.id}

    async def transcript(
        self, organizer: str, subject: str, lines: list[tuple[str, str]], minutes: int
    ) -> dict[str, Any]:
        organizer_id = user_id(organizer)
        meeting_id = "MSo" + secrets.token_urlsafe(24)
        transcript_id = "MSMjMCMj" + secrets.token_urlsafe(24)
        begin = time.time() - minutes * 60
        self.meetings[meeting_id] = {
            "organizer": organizer_id,
            "body": {
                "id": meeting_id,
                "subject": subject,
                "startDateTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(begin)),
                "endDateTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time())),
                "participants": {
                    "organizer": {
                        "upn": company.person(organizer).email,
                        "role": "presenter",
                        "identity": {
                            "user": {
                                "id": organizer_id,
                                "displayName": company.person(organizer).name,
                                "tenantId": MS_TENANT,
                            }
                        },
                    },
                    "attendees": [
                        {
                            "upn": company.person(k).email,
                            "identity": {"user": {"id": user_id(k), "tenantId": MS_TENANT}},
                        }
                        for k in dict.fromkeys(k for k, _ in lines)
                        if k != organizer
                    ],
                },
            },
        }
        self.transcripts[transcript_id] = teams_vtt(lines, minutes)
        resource = (
            f"users('{organizer_id}')/onlineMeetings('{meeting_id}')/transcripts('{transcript_id}')"
        )
        results = []
        for sub in list(self.subscriptions.values()):
            note = {
                "value": [
                    {
                        "subscriptionId": sub["id"],
                        "changeType": "created",
                        "clientState": sub.get("clientState"),
                        "subscriptionExpirationDateTime": sub.get("expirationDateTime"),
                        "resource": resource,
                        "resourceData": {
                            "id": transcript_id,
                            "@odata.type": "#Microsoft.Graph.callTranscript",
                            "@odata.id": resource,
                        },
                        "tenantId": MS_TENANT,
                    }
                ]
            }
            status, answer = 0, ""
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(sub["notificationUrl"], json=note) as resp:
                        status, answer = resp.status, await resp.text()
            except aiohttp.ClientError as err:
                answer = str(err)
            results.append(
                self.deliveries.add(
                    service="teams", event="transcript", status=status, response=answer[:1000]
                )
            )
        return {"resource": resource, "notifications": results}

    def state(self) -> dict[str, Any]:
        return {
            "ms_tenant_id": MS_TENANT,
            "team_id": TEAM_ID,
            "channels": {c.key: c.id for c in CHANNELS},
            "consents": self.consents,
            "subscriptions": [
                {k: v for k, v in sub.items() if k != "clientState"}
                for sub in self.subscriptions.values()
            ],
            "client_states": [sub.get("clientState") for sub in self.subscriptions.values()],
            "graph_calls": self.graph_calls[-200:],
        }
