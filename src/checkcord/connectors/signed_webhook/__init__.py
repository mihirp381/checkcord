"""A generic webhook: any system that can sign a JSON body with a shared secret."""

import argparse

from checkcord.connectors import installations
from checkcord.connectors.base import Connector, Setup, SetupContext
from checkcord.connectors.cli import (
    add_rotate,
    generate_secret,
    print_review_note,
    print_secret_once,
)
from checkcord.connectors.signed_webhook.routes import NAME, router
from checkcord.storage.models import ConnectorInstallation


def _arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--name", required=True, help="a name for the sending system, such as status-page"
    )
    add_rotate(p, "signing secret")


async def _setup(ctx: SetupContext) -> None:
    secret = generate_secret()
    saved = await installations.save(
        ctx.engine,
        ctx.tenant_id,
        NAME,
        ctx.args.name,
        secret=secret,
        replace_secret=ctx.args.rotate_secret,
        review=ctx.review,
    )
    shown = secret if saved.created or ctx.args.rotate_secret else None
    name = ctx.args.name.strip()
    print(
        f"Webhook {name} {'connected' if saved.created else 'updated'} for tenant {ctx.tenant_id}."
    )
    print_review_note(ctx, f"{NAME}:{name}")
    print_secret_once("Signing secret", shown)
    print("Send each event as a JSON POST to:")
    print(f"  URL:     {ctx.public_url}/api/v1/webhooks/signed/{saved.id}")
    print("  Headers: X-Checkcord-Timestamp: <unix seconds>")
    print("           X-Checkcord-Signature: sha256=<hex HMAC-SHA256 of '<timestamp>.<body>'>")
    print('  Body:    {"id": "...", "text": "...", "audience": {"emails": ["..."]}}')


CONNECTORS = (
    Connector(
        name=NAME,
        title="Signed webhook",
        ingestion="webhook",
        status="experimental",
        scope="the container field of each event",
        table=ConnectorInstallation,
        identifier="identifier",
        secret_columns=("secret",),
        routers=((router, "webhooks"),),
        setup=Setup("connect any system that can sign a JSON webhook", _setup, _arguments),
    ),
)
