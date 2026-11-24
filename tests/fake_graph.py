"""An in-memory Microsoft Graph for subscription tests: token, create, renew and delete."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx


class FakeGraph:
    def __init__(self) -> None:
        self.subscriptions: dict[str, dict[str, str]] = {}
        self.calls: list[tuple[str, str]] = []
        self.hosts: set[str] = set()
        self.limited = 0
        self.fail_token = False

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        self.hosts.add(request.url.host)
        if request.url.path.endswith("/oauth2/v2.0/token"):
            if self.fail_token:
                return httpx.Response(401, json={"error": "invalid_client"})
            return httpx.Response(200, json={"access_token": "graph-" + uuid4().hex})
        if self.limited:
            self.limited -= 1
            return httpx.Response(429, headers={"Retry-After": "0"})
        if not request.headers.get("Authorization", "").startswith("Bearer graph-"):
            return httpx.Response(401)
        parts = request.url.path.split("/")
        sub_id = parts[3] if len(parts) > 3 else None
        if request.method == "POST" and sub_id is None:
            body = json.loads(request.content)
            created = {"id": str(uuid4()), **body}
            self.subscriptions[created["id"]] = created
            return httpx.Response(201, json=created)
        if sub_id not in self.subscriptions:
            return httpx.Response(404, json={"error": {"code": "ResourceNotFound"}})
        if request.method == "PATCH":
            self.subscriptions[sub_id].update(json.loads(request.content))
            return httpx.Response(200, json=self.subscriptions[sub_id])
        if request.method == "DELETE":
            del self.subscriptions[sub_id]
            return httpx.Response(204)
        return httpx.Response(405)

    def expires(self, sub_id: str) -> datetime:
        raw = self.subscriptions[sub_id]["expirationDateTime"]
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def soon(hours: float) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)
