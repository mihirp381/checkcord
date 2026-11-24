import asyncio
import hashlib
import hmac
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sim import company
from sim.world import Config, World
from sim.world import slack as slack_module
from sim.world.model_proxy import valid_mode

GITHUB_TOKEN = "ghp-test-token"


def _config() -> Config:
    return Config(
        port=0,
        public_base="http://127.0.0.1:1",
        checkcord_url="http://127.0.0.1:1",
        slack_client_id="cid",
        slack_client_secret="csecret",
        slack_app_token="xapp-test",
        github_token=GITHUB_TOKEN,
        jira_token="jira-test",
        oidc_client_id="oidc",
        oidc_client_secret="oidc-secret",
        model_upstream="http://127.0.0.1:1/v1",
    )


@pytest_asyncio.fixture
async def world() -> AsyncIterator[tuple[World, TestClient[Any, Any]]]:
    w = World(_config())
    client = TestClient(TestServer(w.app()))
    await client.start_server()
    try:
        yield w, client
    finally:
        await client.close()


@pytest_asyncio.fixture
async def receiver() -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
    got: list[dict[str, Any]] = []

    async def hook(request: web.Request) -> web.Response:
        got.append(
            {"path": request.path, "headers": dict(request.headers), "body": await request.read()}
        )
        return web.json_response({"ok": True}, status=202)

    app = web.Application()
    app.router.add_post("/{tail:.*}", hook)
    server = TestServer(app)
    await server.start_server()
    try:
        yield str(server.make_url("/")), got
    finally:
        await server.close()


def test_model_modes() -> None:
    assert valid_mode("ok") and valid_mode("down") and valid_mode("garbage")
    assert valid_mode("slow:2.5")
    assert not valid_mode("slow:x") and not valid_mode("sideways")


@pytest.mark.asyncio
async def test_model_down_and_garbage_are_counted(
    world: tuple[World, TestClient[Any, Any]],
) -> None:
    w, client = world
    r = await client.post("/control/model/mode", json={"mode": "down"})
    assert r.status == 200
    r = await client.post("/model/v1/chat/completions", json={"messages": []})
    assert r.status == 500
    await client.post("/control/model/mode", json={"mode": "garbage"})
    r = await client.post("/model/v1/embeddings", json={"input": "x"})
    assert r.status == 200
    with pytest.raises(ValueError):
        await r.json(content_type=None)
    summary = await (await client.get("/control/model")).json()
    assert summary["failures"] == {"chat": 1, "embeddings": 1}
    assert [b["mode"] for b in summary["log"]] == ["down", "garbage"]
    r = await client.post("/control/model/mode", json={"mode": "sideways"})
    assert r.status == 400


@pytest.mark.asyncio
async def test_slack_web_api_auth_pagination_and_rate_limit(
    world: tuple[World, TestClient[Any, Any]],
) -> None:
    w, client = world
    r = await client.post("/api/conversations.list", data={"token": "xoxb-nope"})
    assert (await r.json())["error"] == "invalid_auth"
    w.slack.bot_tokens.add("xoxb-good")
    headers = {"Authorization": "Bearer xoxb-good"}
    names, cursor = [], ""
    pages = 0
    while True:
        r = await client.post(
            "/api/conversations.list",
            data={"types": "public_channel,private_channel", "cursor": cursor, "limit": 200},
            headers=headers,
        )
        body = await r.json()
        pages += 1
        names += [c["name"] for c in body["channels"]]
        cursor = body["response_metadata"]["next_cursor"]
        if not cursor:
            break
    assert sorted(names) == sorted(c.name for c in company.CHANNELS)
    assert pages > 1
    await client.post("/control/slack/fault", json={"count": 1})
    r = await client.post("/api/auth.test", headers=headers)
    assert r.status == 429 and r.headers["Retry-After"] == "1"
    r = await client.post("/api/auth.test", headers=headers)
    assert r.status == 200 and (await r.json())["ok"]


@pytest.mark.asyncio
async def test_slack_oauth_code_is_single_use(world: tuple[World, TestClient[Any, Any]]) -> None:
    w, client = world
    r = await client.get(
        "/oauth/v2/authorize",
        params={"client_id": "cid", "redirect_uri": "http://x/cb", "state": "s1"},
        allow_redirects=False,
    )
    code = r.headers["Location"].split("code=")[1].split("&")[0]
    form = {"client_id": "cid", "client_secret": "csecret", "code": code}
    first = await (await client.post("/api/oauth.v2.access", data=form)).json()
    assert first["ok"] and first["access_token"].startswith("xoxb-")
    second = await (await client.post("/api/oauth.v2.access", data=form)).json()
    assert second == {"ok": False, "error": "invalid_code"}


@pytest.mark.asyncio
async def test_socket_mode_redelivers_until_acked(
    world: tuple[World, TestClient[Any, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(slack_module, "ACK_SECONDS", 0.2)
    w, client = world
    ws = await client.ws_connect("/slack/socket")
    assert (await ws.receive_json())["type"] == "hello"
    await client.post(
        "/control/slack/post", json={"channel": "eng", "person": "tomas", "text": "hi"}
    )
    first = await ws.receive_json(timeout=5)
    assert first["retry_attempt"] == 0
    assert first["payload"]["event"]["text"] == "hi"
    again = await ws.receive_json(timeout=5)
    assert again["retry_attempt"] == 1 and again["retry_reason"] == "timeout"
    await ws.send_json({"envelope_id": again["envelope_id"]})
    await asyncio.sleep(0.5)
    assert again["envelope_id"] in {a["envelope_id"] for a in w.slack.acks}
    assert not w.slack.unacked
    await ws.close()


def _valid(secret: str, body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


@pytest.mark.asyncio
async def test_github_webhooks_are_signed_and_rest_needs_the_token(
    world: tuple[World, TestClient[Any, Any]], receiver: tuple[str, list[dict[str, Any]]]
) -> None:
    w, client = world
    base, got = receiver
    await client.post(
        "/control/github/register",
        json={"repo": company.PAYMENTS, "url": base + "hooks/github", "secret": "gh-secret"},
    )
    pr = await (
        await client.post(
            "/control/github/pull",
            json={
                "repo": company.PAYMENTS,
                "author": "tomas",
                "title": "t",
                "changes": {"README.md": "x"},
            },
        )
    ).json()
    assert pr["delivery"]["status"] == 202
    hook = got[0]
    assert hook["headers"]["X-GitHub-Event"] == "pull_request"
    assert _valid("gh-secret", hook["body"], hook["headers"]["X-Hub-Signature-256"])
    path = f"/github/repos/{company.PAYMENTS}/pulls/{pr['number']}/files"
    assert (await client.get(path)).status == 401
    r = await client.get(path, headers={"Authorization": f"Bearer {GITHUB_TOKEN}"})
    assert r.status == 200
    assert [f["filename"] for f in await r.json()] == ["README.md"]


@pytest.mark.asyncio
async def test_jira_issue_sends_a_signed_webhook(
    world: tuple[World, TestClient[Any, Any]], receiver: tuple[str, list[dict[str, Any]]]
) -> None:
    w, client = world
    base, got = receiver
    await client.post("/control/jira/register", json={"url": base + "jira", "secret": "j-secret"})
    await client.post(
        "/control/jira/issue",
        json={"key": "PAY-1", "summary": "s", "description": "d", "reporter": "tomas"},
    )
    assert _valid("j-secret", got[0]["body"], got[0]["headers"]["X-Hub-Signature-256"])
    r = await client.get("/rest/api/3/issue/PAY-1")
    assert r.status == 401
    r = await client.get("/rest/api/3/issue/PAY-1", headers={"Authorization": "Bearer jira-test"})
    assert (await r.json())["key"] == "PAY-1"
    deliveries = await (await client.get("/control/deliveries")).json()
    assert deliveries["items"][0]["event"] == "jira:issue_created"
