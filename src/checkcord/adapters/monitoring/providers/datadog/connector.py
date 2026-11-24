import argparse
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.monitoring.providers.datadog.webhook import router
from checkcord.connectors.base import Connector, Setup, SetupContext
from checkcord.connectors.cli import (
    ConnectResult,
    add_rotate,
    generate_secret,
    generated,
    print_review_note,
    print_secret_once,
    upsert_per_tenant,
)
from checkcord.storage.models import DatadogInstallation


async def connect_datadog(
    engine: AsyncEngine, tenant_id: UUID, rotate_secret: bool, review: bool = False
) -> ConnectResult:
    secret = generate_secret()
    installation_id, created = await upsert_per_tenant(
        engine,
        tenant_id,
        "datadog",
        DatadogInstallation,
        secret,
        secret if rotate_secret else None,
        review,
    )
    return generated(installation_id, created, secret, rotate_secret)


def webhook_url(base_url: str, installation_id: UUID) -> str:
    return f"{base_url.rstrip('/')}/api/v1/webhooks/datadog/{installation_id}"


async def _setup(ctx: SetupContext) -> None:
    result = await connect_datadog(ctx.engine, ctx.tenant_id, ctx.args.rotate_secret, ctx.review)
    print(f"Datadog {'connected' if result.created else 'updated'} for tenant {ctx.tenant_id}.")
    print_review_note(ctx, f"datadog:{result.installation_id}")
    print_secret_once("Webhook token", result.generated_secret)
    print("In Datadog: Integrations > Webhooks > New:")
    url = webhook_url(ctx.public_url, result.installation_id)
    print(f"  URL:            {url}")
    print('  Custom headers: {"X-Datadog-Token": "<the webhook token above>"}')
    print(
        '  Payload:        {"id": "$ID", "title": "$EVENT_TITLE", "body": "$EVENT_MSG", '
        '"event_type": "$EVENT_TYPE", "url": "$LINK", "date": "$DATE"}'
    )
    print("Then add @webhook-<name> to the monitors that should report to Checkcord.")


def _arguments(p: argparse.ArgumentParser) -> None:
    add_rotate(p, "webhook token")


CONNECTORS = (
    Connector(
        name="datadog",
        title="Datadog",
        ingestion="webhook",
        status="experimental",
        audience_from="admin",
        table=DatadogInstallation,
        identifier="id",
        secret_columns=("webhook_secret",),
        routers=((router, "webhooks"),),
        setup=Setup("connect Datadog monitor alerts", _setup, _arguments),
    ),
)
