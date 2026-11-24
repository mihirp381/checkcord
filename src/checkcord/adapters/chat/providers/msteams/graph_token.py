"""Microsoft Graph app token for a Teams tenant."""

from __future__ import annotations

import aiohttp

from checkcord.core.config import get_settings


async def get_msteams_access_token(
    ms_tenant_id: str, client_id: str | None, client_secret: str | None
) -> str:
    """Retrieve an access token using MS Graph client credentials flow."""
    if not client_id or not client_secret:
        raise ValueError("msteams_client_id and msteams_client_secret must be configured")

    settings = get_settings()
    url = f"{settings.ms_login_url}/{ms_tenant_id}/oauth2/v2.0/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": settings.graph_scope,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise Exception(
                    f"Failed to retrieve MS Teams access token: HTTP {resp.status} - {body}"
                )
            data = await resp.json()
            token = data.get("access_token")
            if not token:
                raise Exception("Access token missing in token endpoint response")
            return str(token)
