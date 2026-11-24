import argparse
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.meetings.providers.msteams import subscriptions
from checkcord.adapters.meetings.providers.msteams.webhook import router
from checkcord.connectors.base import Connector, Poll, Setup, SetupContext
from checkcord.connectors.cli import (
    ConnectResult,
    add_rotate,
    generate_secret,
    generated,
    print_review_note,
    upsert_by_natural_key,
)
from checkcord.core.config import Settings
from checkcord.storage.models import MsTeamsMeetingInstallation

SUBSCRIPTION_INTERVAL_MINUTES = 60


async def connect_teams_meetings(
    engine: AsyncEngine,
    tenant_id: UUID,
    ms_tenant_id: str,
    rotate_secret: bool,
    review: bool = False,
) -> ConnectResult:
    secret = generate_secret()
    installation_id, created = await upsert_by_natural_key(
        engine,
        tenant_id,
        "teams-meetings",
        MsTeamsMeetingInstallation,
        MsTeamsMeetingInstallation.ms_tenant_id,
        ms_tenant_id.strip(),
        {},
        secret,
        secret if rotate_secret else None,
        review,
    )
    return generated(installation_id, created, secret, rotate_secret)


def _arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--ms-tenant-id", required=True, help="Microsoft Entra tenant id")
    add_rotate(p, "clientState")


async def _setup(ctx: SetupContext) -> None:
    from checkcord.core.config import get_settings

    ms_tenant_id = ctx.args.ms_tenant_id.strip()
    settings = get_settings().model_copy(update={"public_base_url": ctx.public_url})
    result = await connect_teams_meetings(
        ctx.engine, ctx.tenant_id, ms_tenant_id, ctx.args.rotate_secret, ctx.review
    )
    subscription, error = await subscriptions.ensure(
        ctx.engine, ctx.tenant_id, ms_tenant_id, settings=settings
    )
    print(f"Teams meetings for {ms_tenant_id} {'connected' if result.created else 'updated'}.")
    print_review_note(ctx, f"teams-meetings:{ms_tenant_id}")
    if subscription is not None:
        expires = f"{subscription.expires_at:%Y-%m-%d %H:%M} UTC"
        print(f"Graph subscription {subscription.id} created; it expires {expires}.")
    else:
        print(f"Graph subscription not created yet: {error}")
    print("The API checks it every hour and renews it a day before it expires.")
    print("`checkcord doctor` shows its state. The API needs PUBLIC_BASE_URL, MSTEAMS_CLIENT_ID")
    print("and MSTEAMS_CLIENT_SECRET to keep it and to fetch transcripts.")


async def _renew(engine: AsyncEngine, _bus: Any) -> int:
    return await subscriptions.renew_due(engine)


async def _disconnect(row: Mapping[str, Any], settings: Settings) -> None:
    await subscriptions.remove(row["ms_tenant_id"], row["subscription_id"], settings=settings)


CONNECTORS = (
    Connector(
        name="teams-meetings",
        title="Teams meetings",
        ingestion="Graph change notifications",
        status="experimental",
        table=MsTeamsMeetingInstallation,
        identifier="ms_tenant_id",
        secret_columns=("webhook_secret",),
        routers=((router, "webhooks"),),
        poll=Poll(
            "renew_teams_meeting_subscriptions",
            _renew,
            lambda _: SUBSCRIPTION_INTERVAL_MINUTES,
            publishes=False,
        ),
        setup=Setup("connect Microsoft Teams meeting transcripts", _setup, _arguments),
        disconnect=_disconnect,
    ),
)
