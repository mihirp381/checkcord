"""The Microsoft Graph subscription that sends Teams meeting transcripts to Checkcord,
kept renewed."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.core import http_retry
from checkcord.core.config import Settings, get_settings
from checkcord.core.crypto import decrypt_secret
from checkcord.storage.models import MsTeamsMeetingInstallation
from checkcord.storage.postgres import app_bypass_rls_context, tenant_context

logger = structlog.get_logger(__name__)

RESOURCE = "communications/onlineMeetings/getAllTranscripts"
WEBHOOK_PATH = "/api/v1/webhooks/msteams/meetings"
LIFECYCLE_PATH = f"{WEBHOOK_PATH}/lifecycle"
# Graph allows at most three days for transcript subscriptions.
LIFETIME = timedelta(hours=70)
RENEW_BEFORE = timedelta(hours=24)
TIMEOUT_SECONDS = 30.0


class GraphError(Exception):
    pass


@dataclass(frozen=True)
class Subscription:
    id: str
    expires_at: datetime


@asynccontextmanager
async def _client(client: httpx.AsyncClient | None) -> AsyncIterator[httpx.AsyncClient]:
    if client is not None:
        yield client
        return
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as own:
        yield own


def _configured(settings: Settings) -> tuple[str, str, str]:
    if not settings.msteams_client_id or not settings.msteams_client_secret:
        raise GraphError("MSTEAMS_CLIENT_ID and MSTEAMS_CLIENT_SECRET are not set")
    if not settings.public_base_url:
        raise GraphError("PUBLIC_BASE_URL is not set")
    return (
        settings.msteams_client_id,
        settings.msteams_client_secret,
        settings.public_base_url.rstrip("/"),
    )


async def _token(client: httpx.AsyncClient, ms_tenant_id: str, settings: Settings) -> str:
    client_id, client_secret, _ = _configured(settings)
    resp = await http_retry.send(
        "microsoft",
        client,
        "POST",
        f"{settings.ms_login_url}/{ms_tenant_id}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": settings.graph_scope,
        },
    )
    if resp.status_code != 200:
        raise GraphError(f"token request answered {resp.status_code}")
    return str(resp.json()["access_token"])


async def _graph(
    client: httpx.AsyncClient,
    settings: Settings,
    method: str,
    path: str,
    token: str,
    **kwargs: Any,
) -> httpx.Response:
    return await http_retry.send(
        "microsoft",
        client,
        method,
        f"{settings.graph_api}{path}",
        headers={"Authorization": f"Bearer {token}"},
        **kwargs,
    )


def _parse(body: dict[str, Any]) -> Subscription:
    expires = datetime.fromisoformat(str(body["expirationDateTime"]).replace("Z", "+00:00"))
    return Subscription(str(body["id"]), expires)


def _expiry(now: datetime) -> str:
    return (now + LIFETIME).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def create(
    client: httpx.AsyncClient,
    ms_tenant_id: str,
    client_state: str,
    settings: Settings,
    now: datetime | None = None,
) -> Subscription:
    _, _, base = _configured(settings)
    token = await _token(client, ms_tenant_id, settings)
    resp = await _graph(
        client,
        settings,
        "POST",
        "/subscriptions",
        token,
        json={
            "changeType": "created",
            "notificationUrl": f"{base}{WEBHOOK_PATH}",
            "lifecycleNotificationUrl": f"{base}{LIFECYCLE_PATH}",
            "resource": RESOURCE,
            "expirationDateTime": _expiry(now or datetime.now(UTC)),
            "clientState": client_state,
        },
    )
    if resp.status_code != 201:
        raise GraphError(f"creating the subscription answered {resp.status_code}")
    return _parse(resp.json())


async def renew(
    client: httpx.AsyncClient,
    ms_tenant_id: str,
    subscription_id: str,
    client_state: str,
    settings: Settings,
    now: datetime | None = None,
) -> Subscription:
    """Extend the subscription; one that Graph no longer has is created again."""
    token = await _token(client, ms_tenant_id, settings)
    resp = await _graph(
        client,
        settings,
        "PATCH",
        f"/subscriptions/{subscription_id}",
        token,
        json={"expirationDateTime": _expiry(now or datetime.now(UTC))},
    )
    if resp.status_code == 404:
        return await create(client, ms_tenant_id, client_state, settings, now)
    if resp.status_code != 200:
        raise GraphError(f"renewing the subscription answered {resp.status_code}")
    return _parse(resp.json())


async def delete(
    client: httpx.AsyncClient, ms_tenant_id: str, subscription_id: str, settings: Settings
) -> None:
    token = await _token(client, ms_tenant_id, settings)
    resp = await _graph(client, settings, "DELETE", f"/subscriptions/{subscription_id}", token)
    if resp.status_code not in (204, 404):
        raise GraphError(f"deleting the subscription answered {resp.status_code}")


async def _store(
    engine: AsyncEngine,
    row: Any,
    subscription: Subscription | None,
    error: str | None,
) -> None:
    values: dict[str, Any] = {"subscription_error": error}
    if subscription is not None:
        values |= {
            "subscription_id": subscription.id,
            "subscription_expires_at": subscription.expires_at,
        }
    async with tenant_context(engine, row["tenant_id"]) as conn:
        await conn.execute(
            update(MsTeamsMeetingInstallation)
            .where(MsTeamsMeetingInstallation.id == row["id"])
            .values(**values)
        )


async def _keep(
    engine: AsyncEngine,
    client: httpx.AsyncClient,
    row: Any,
    settings: Settings,
    now: datetime,
) -> Subscription | None:
    try:
        client_state = decrypt_secret(row["webhook_secret"])
        if row["subscription_id"]:
            found = await renew(
                client, row["ms_tenant_id"], row["subscription_id"], client_state, settings, now
            )
        else:
            found = await create(client, row["ms_tenant_id"], client_state, settings, now)
    except Exception as err:
        reason = str(err) or type(err).__name__
        logger.warning("teams_subscription.failed", error=reason)
        await _store(engine, row, None, reason)
        return None
    await _store(engine, row, found, None)
    logger.info("teams_subscription.kept", expires_at=found.expires_at.isoformat())
    return found


async def ensure(
    engine: AsyncEngine,
    tenant_id: UUID,
    ms_tenant_id: str,
    client: httpx.AsyncClient | None = None,
    settings: Settings | None = None,
) -> tuple[Subscription | None, str | None]:
    """Create or renew the connection's subscription now; the subscription or the reason."""
    async with tenant_context(engine, tenant_id) as conn:
        row = (
            (
                await conn.execute(
                    select(MsTeamsMeetingInstallation).where(
                        MsTeamsMeetingInstallation.ms_tenant_id == ms_tenant_id
                    )
                )
            )
            .mappings()
            .one()
        )
    async with _client(client) as http:
        found = await _keep(engine, http, row, settings or get_settings(), datetime.now(UTC))
    if found is not None:
        return found, None
    async with tenant_context(engine, tenant_id) as conn:
        error = await conn.scalar(
            select(MsTeamsMeetingInstallation.subscription_error).where(
                MsTeamsMeetingInstallation.id == row["id"]
            )
        )
    return None, error


async def renew_due(
    engine: AsyncEngine,
    client: httpx.AsyncClient | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> int:
    """Create missing subscriptions and renew those expiring within RENEW_BEFORE; how many kept."""
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    async with app_bypass_rls_context(engine) as conn:
        rows = (
            (
                await conn.execute(
                    select(MsTeamsMeetingInstallation).where(
                        (MsTeamsMeetingInstallation.subscription_id.is_(None))
                        | (MsTeamsMeetingInstallation.subscription_expires_at.is_(None))
                        | (MsTeamsMeetingInstallation.subscription_expires_at < now + RENEW_BEFORE)
                    )
                )
            )
            .mappings()
            .all()
        )
    if not rows:
        return 0
    if not settings.msteams_client_id or not settings.public_base_url:
        logger.warning("teams_subscription.not_configured", connections=len(rows))
    kept = 0
    async with _client(client) as http:
        for row in rows:
            if await _keep(engine, http, row, settings, now) is not None:
                kept += 1
    return kept


async def remove(
    ms_tenant_id: str,
    subscription_id: str | None,
    client: httpx.AsyncClient | None = None,
    settings: Settings | None = None,
) -> None:
    """Delete the Graph subscription when a connection is removed; failures are only logged."""
    if not subscription_id:
        return
    try:
        async with _client(client) as http:
            await delete(http, ms_tenant_id, subscription_id, settings or get_settings())
    except (GraphError, httpx.HTTPError, KeyError, ValueError) as err:
        logger.warning("teams_subscription.delete_failed", error=str(err) or type(err).__name__)


async def renew_for(
    engine: AsyncEngine,
    ms_tenant_id: str,
    recreate: bool,
    client: httpx.AsyncClient | None = None,
) -> Subscription | None:
    """Act on a lifecycle notification: renew, or create again when Graph removed it."""
    async with app_bypass_rls_context(engine) as conn:
        row = (
            (
                await conn.execute(
                    select(MsTeamsMeetingInstallation).where(
                        MsTeamsMeetingInstallation.ms_tenant_id == ms_tenant_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        return None
    if recreate:
        row = {**row, "subscription_id": None}
    async with _client(client) as http:
        return await _keep(engine, http, row, get_settings(), datetime.now(UTC))
