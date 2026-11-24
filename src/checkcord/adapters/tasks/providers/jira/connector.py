import argparse
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.tasks.providers.jira.document_poller import poll_jira_documents
from checkcord.adapters.tasks.providers.jira.webhook import router
from checkcord.connectors.base import Connector, Poll, Setup, SetupContext
from checkcord.connectors.cli import (
    ConnectResult,
    add_rotate,
    generate_secret,
    generated,
    print_review_note,
    print_secret_once,
    read_secret,
    upsert_by_natural_key,
)
from checkcord.core.crypto import encrypt_secret
from checkcord.storage.models import JiraInstallation, JiraProjectKey
from checkcord.storage.postgres import tenant_context


def normalize_jira_host(host: str, *, allow_http: bool | None = None) -> str:
    """The stored host: its netloc, or a full ``http://`` base URL in development."""
    from checkcord.core.config import get_settings

    host = host.strip()
    parsed = urlsplit(host if "://" in host else f"https://{host}")
    if not parsed.netloc or parsed.scheme not in ("http", "https"):
        raise ValueError(f"not a Jira host: {host!r}")
    if parsed.scheme == "http":
        if allow_http is None:
            allow_http = get_settings().environment == "development"
        if not allow_http:
            raise ValueError("Jira needs https; an http:// address works only in development")
        return f"http://{parsed.netloc.lower()}"
    return parsed.netloc.lower()


def webhook_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/v1/webhooks/jira"


async def connect_jira(
    engine: AsyncEngine,
    tenant_id: UUID,
    host: str,
    token: str,
    rotate_secret: bool,
    project_keys: list[str],
    review: bool = False,
) -> ConnectResult:
    secret = generate_secret()
    installation_id, created = await upsert_by_natural_key(
        engine,
        tenant_id,
        "jira",
        JiraInstallation,
        JiraInstallation.jira_host,
        normalize_jira_host(host),
        {"personal_access_token": encrypt_secret(token)},
        secret,
        secret if rotate_secret else None,
        review,
    )
    if project_keys:
        async with tenant_context(engine, tenant_id) as conn:
            for key in project_keys:
                stmt = pg_insert(JiraProjectKey).values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    jira_installation_id=installation_id,
                    project_key=key.strip().upper(),
                )
                await conn.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["tenant_id", "project_key"],
                        set_={
                            "jira_installation_id": stmt.excluded.jira_installation_id,
                            "updated_at": func.now(),
                        },
                    )
                )
    return generated(installation_id, created, secret, rotate_secret)


def _arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", required=True, help="Jira host, e.g. acme.atlassian.net")
    p.add_argument("--token", help="personal access token (or CHECKCORD_JIRA_TOKEN, or prompt)")
    p.add_argument(
        "--project-key",
        action="append",
        default=[],
        metavar="KEY",
        help="Jira project key to ingest (repeatable)",
    )
    add_rotate(p, "webhook secret")


async def _setup(ctx: SetupContext) -> None:
    args = ctx.args
    token = read_secret(args.token, "CHECKCORD_JIRA_TOKEN", "Jira personal access token")
    result = await connect_jira(
        ctx.engine,
        ctx.tenant_id,
        args.host,
        token,
        args.rotate_secret,
        args.project_key,
        ctx.review,
    )
    print(
        f"Jira {args.host} {'connected' if result.created else 'updated'}"
        f" for tenant {ctx.tenant_id}."
    )
    print_review_note(ctx, f"jira:{normalize_jira_host(args.host)}")
    print_secret_once("Webhook secret", result.generated_secret)
    print("In Jira: Settings > System > WebHooks > Create a WebHook:")
    print(f"  URL:    {webhook_url(ctx.public_url)}")
    print("  Secret: the webhook secret above")
    print("  Events: Issue created/updated/deleted, Comment created/updated")


CONNECTORS = (
    Connector(
        name="jira",
        title="Jira",
        ingestion="webhook and poller",
        status="experimental",
        audience_from="admin",
        scope="Jira project keys",
        table=JiraInstallation,
        identifier="jira_host",
        secret_columns=("personal_access_token", "webhook_secret"),
        routers=((router, "webhooks"),),
        poll=Poll(
            "poll_jira_documents",
            poll_jira_documents,
            lambda s: s.task_document_poll_interval_minutes,
        ),
        setup=Setup("connect a Jira site", _setup, _arguments),
    ),
)
