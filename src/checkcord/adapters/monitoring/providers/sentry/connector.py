import argparse
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.monitoring.providers.sentry.webhook import router
from checkcord.connectors.base import Connector, Setup, SetupContext
from checkcord.connectors.cli import (
    ConnectResult,
    print_review_note,
    read_secret,
    upsert_per_tenant,
)
from checkcord.storage.models import SentryInstallation


async def connect_sentry(
    engine: AsyncEngine, tenant_id: UUID, client_secret: str, review: bool = False
) -> ConnectResult:
    installation_id, created = await upsert_per_tenant(
        engine, tenant_id, "sentry", SentryInstallation, client_secret, client_secret, review
    )
    return ConnectResult(installation_id, created, None)


def webhook_url(base_url: str, installation_id: UUID | str) -> str:
    return f"{base_url.rstrip('/')}/api/v1/webhooks/sentry/{installation_id}"


def _arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--client-secret",
        help="integration client secret (or CHECKCORD_SENTRY_CLIENT_SECRET, or prompt)",
    )


async def _setup(ctx: SetupContext) -> None:
    base = ctx.public_url
    print("In Sentry: Settings > Developer Settings > Custom Integrations > Internal Integration:")
    print(f"  Webhook URL: {webhook_url(base, '<installation id>')}")
    secret = read_secret(
        ctx.args.client_secret, "CHECKCORD_SENTRY_CLIENT_SECRET", "Sentry client secret"
    )
    result = await connect_sentry(ctx.engine, ctx.tenant_id, secret, ctx.review)
    print(f"Sentry {'connected' if result.created else 'updated'} for tenant {ctx.tenant_id}.")
    print_review_note(ctx, f"sentry:{result.installation_id}")
    print(f"  Webhook URL:  {webhook_url(base, result.installation_id)}")
    print("  Enable webhooks for issue and error events, and Alert Rule Action if wanted.")
    print(
        "  Checkcord verifies the Sentry-Hook-Signature header with the client secret you entered."
    )


CONNECTORS = (
    Connector(
        name="sentry",
        title="Sentry",
        ingestion="webhook",
        status="experimental",
        audience_from="admin",
        table=SentryInstallation,
        identifier="id",
        secret_columns=("webhook_secret",),
        routers=((router, "webhooks"),),
        setup=Setup("connect a Sentry internal integration", _setup, _arguments),
    ),
)
