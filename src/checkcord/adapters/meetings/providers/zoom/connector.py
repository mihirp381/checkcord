import argparse
from urllib.parse import quote
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.meetings.providers.zoom.webhook import router
from checkcord.connectors.base import Connector, Setup, SetupContext
from checkcord.connectors.cli import (
    ConnectResult,
    print_review_note,
    read_secret,
    upsert_by_natural_key,
)
from checkcord.storage.models import ZoomInstallation


async def connect_zoom(
    engine: AsyncEngine, tenant_id: UUID, account_id: str, secret_token: str, review: bool = False
) -> ConnectResult:
    installation_id, created = await upsert_by_natural_key(
        engine,
        tenant_id,
        "zoom",
        ZoomInstallation,
        ZoomInstallation.zoom_account_id,
        account_id.strip(),
        {},
        secret_token,
        secret_token,
        review,
    )
    return ConnectResult(installation_id, created, None)


def webhook_url(base_url: str, account_id: str) -> str:
    return f"{base_url.rstrip('/')}/api/v1/webhooks/zoom?zoom_account_id={quote(account_id)}"


def _arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--account-id", required=True, help="Zoom account id")
    p.add_argument(
        "--secret-token", help="webhook secret token (or CHECKCORD_ZOOM_SECRET_TOKEN, or prompt)"
    )


async def _setup(ctx: SetupContext) -> None:
    account_id = ctx.args.account_id
    secret = read_secret(
        ctx.args.secret_token, "CHECKCORD_ZOOM_SECRET_TOKEN", "Zoom webhook secret token"
    )
    result = await connect_zoom(ctx.engine, ctx.tenant_id, account_id, secret, ctx.review)
    print(f"Zoom account {account_id} {'connected' if result.created else 'updated'}.")
    print_review_note(ctx, f"zoom:{account_id.strip()}")
    print("In the Zoom App Marketplace app (Features > Access > Event Subscriptions):")
    print(f"  Event notification endpoint URL: {webhook_url(ctx.public_url, account_id)}")
    print("  Events: Recording > All recordings have completed, Transcript files have completed")
    print("  Then press Validate; Checkcord answers Zoom's URL validation with the secret token.")


CONNECTORS = (
    Connector(
        name="zoom",
        title="Zoom",
        ingestion="webhook",
        status="experimental",
        table=ZoomInstallation,
        identifier="zoom_account_id",
        secret_columns=("webhook_secret",),
        routers=((router, "webhooks"),),
        setup=Setup("connect a Zoom account's recording transcripts", _setup, _arguments),
    ),
)
