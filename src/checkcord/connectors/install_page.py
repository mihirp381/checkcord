"""The page a browser lands on after a connector's OAuth redirect."""

from __future__ import annotations

from html import escape

from fastapi.responses import HTMLResponse
from starlette.background import BackgroundTask

from checkcord.core.config import get_settings

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 36rem; margin: 4rem auto; }}
body {{ padding: 0 1rem; line-height: 1.5; }}
</style>
</head>
<body>
<p>{message}</p>{link}
</body>
</html>
"""


def _page(
    title: str, message: str, status_code: int, background: BackgroundTask | None = None
) -> HTMLResponse:
    base = get_settings().web_base_url
    link = f'\n<p><a href="{escape(base)}">Open the Checkcord console</a></p>' if base else ""
    body = _PAGE.format(title=escape(title), message=escape(message), link=link)
    return HTMLResponse(body, status_code=status_code, background=background)


def connected(provider: str, background: BackgroundTask | None = None) -> HTMLResponse:
    return _page(
        f"{provider} connected",
        f"{provider} is connected to Checkcord. You can close this tab.",
        200,
        background,
    )


def failed(provider: str, message: str, status_code: int) -> HTMLResponse:
    return _page(f"{provider} not connected", message, status_code)
