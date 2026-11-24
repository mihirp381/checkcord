import secrets

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from fastapi import HTTPException

from checkcord.adapters.meetings.providers.zoom.webhook import download_transcript

pytestmark = pytest.mark.asyncio


async def test_download_sends_the_token_as_a_bearer_and_never_echoes_it() -> None:
    token = secrets.token_urlsafe(16)

    async def download(request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {token}":
            return web.json_response({"message": "Invalid access token."}, status=401)
        return web.Response(text="WEBVTT\n")

    app = web.Application()
    app.router.add_get("/rec/download/f1", download)
    async with TestServer(app) as server:
        url = str(server.make_url("/rec/download/f1"))
        assert await download_transcript(url, token) == "WEBVTT\n"
        with pytest.raises(HTTPException) as failed:
            await download_transcript(url, "wrong-" + token)
    assert failed.value.status_code == 500
    assert token not in str(failed.value.detail)
