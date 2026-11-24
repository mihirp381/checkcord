import argparse
import os
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.cicd.providers.github.webhook import router as cicd_router
from checkcord.adapters.code.providers.github.poller import poll_github_history
from checkcord.adapters.code.providers.github.webhook import router
from checkcord.api.repo_router import router as repo_router
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.connectors.base import Connector, Poll, Setup, SetupContext
from checkcord.connectors.cli import (
    ConnectResult,
    generate_secret,
    print_review_note,
    print_secret_once,
    read_secret,
)
from checkcord.schemas.repo_connect import RepoConnectRequest
from checkcord.services import review as reviews
from checkcord.services.connections import connection_key
from checkcord.services.repo_connect import (
    BackfillPublishError,
    RepoOwnedByOtherTenantError,
    request_backfill,
    upsert_github_installation,
)
from checkcord.storage import audit
from checkcord.storage.models import GithubInstallation
from checkcord.storage.postgres import tenant_context


async def connect_github(
    engine: AsyncEngine,
    bus: RedisStreamsEventBus,
    tenant_id: UUID,
    repo: str,
    token: str,
    webhook_secret: str | None,
    review: bool = False,
) -> ConnectResult:
    secret = webhook_secret or generate_secret()
    payload = RepoConnectRequest(
        repo_identifier=repo, personal_access_token=token, webhook_secret=secret
    )
    async with tenant_context(engine, tenant_id) as conn:
        await reviews.mark_new(conn, tenant_id, connection_key("github", repo), review)
    await upsert_github_installation(engine, tenant_id, payload, audit.CLI_ACTOR)
    await request_backfill(bus, tenant_id, repo)
    async with tenant_context(engine, tenant_id) as conn:
        installation_id = await conn.scalar(
            select(GithubInstallation.id).where(GithubInstallation.repo_identifier == repo)
        )
    return ConnectResult(installation_id, True, None if webhook_secret else secret)


def webhook_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/v1/webhooks/github"


def _arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", required=True, metavar="OWNER/NAME", help="repository")
    p.add_argument("--token", help="personal access token (or CHECKCORD_GITHUB_TOKEN, or prompt)")
    p.add_argument(
        "--webhook-secret",
        help="webhook secret (or CHECKCORD_GITHUB_WEBHOOK_SECRET; generated if omitted)",
    )


async def _setup(ctx: SetupContext) -> None:
    from redis.asyncio import Redis

    from checkcord.core.config import get_settings

    repo = ctx.args.repo
    if repo.count("/") != 1 or not all(repo.split("/")):
        raise ValueError("--repo must look like OWNER/NAME")
    token = read_secret(ctx.args.token, "CHECKCORD_GITHUB_TOKEN", "GitHub personal access token")
    webhook_secret = ctx.args.webhook_secret or os.environ.get("CHECKCORD_GITHUB_WEBHOOK_SECRET")

    redis = Redis.from_url(get_settings().redis_url)
    try:
        result = await connect_github(
            ctx.engine,
            RedisStreamsEventBus(redis),
            ctx.tenant_id,
            repo,
            token,
            webhook_secret,
            ctx.review,
        )
    except RepoOwnedByOtherTenantError:
        raise ValueError(f"{repo} is already connected to a different tenant") from None
    except BackfillPublishError:
        raise ValueError(
            "repository saved, but the backfill request could not be queued: is Redis up?"
        ) from None
    finally:
        await redis.aclose()
    print(f"GitHub {repo} connected for tenant {ctx.tenant_id}; full backfill queued.")
    print_review_note(ctx, f"github:{repo}")
    if result.generated_secret:
        print_secret_once("Webhook secret", result.generated_secret)
    print(f"In GitHub: {repo} > Settings > Webhooks > Add webhook:")
    print(f"  Payload URL:  {webhook_url(ctx.public_url)}")
    print("  Content type: application/json")
    print("  Secret:       the webhook secret" + (" above" if result.generated_secret else ""))
    print("  Events:       Pull requests, Pull request reviews, Pull request review comments,")
    print("                Issue comments, Pushes, Workflow runs, Collaborator add, remove,")
    print("                or changed, Repositories")
    print("One webhook is enough; it also handles CI failures from workflow runs.")


CONNECTORS = (
    Connector(
        name="github",
        title="GitHub",
        ingestion="webhook and poller",
        status="beta",
        audience_from="source",
        scope="GitHub repositories (owner/name)",
        audience_platform="github",
        table=GithubInstallation,
        identifier="repo_identifier",
        secret_columns=("personal_access_token", "webhook_secret"),
        routers=((repo_router, None), (router, "webhooks"), (cicd_router, "webhooks")),
        poll=Poll(
            "poll_github_history", poll_github_history, lambda s: s.github_poll_interval_minutes
        ),
        setup=Setup("connect a GitHub repository and queue a backfill", _setup, _arguments),
    ),
)
