import hashlib
import hmac
import json
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.adapters.cicd.providers.github.webhook import handle_workflow_run
from checkcord.adapters.code.providers.github.parser import (
    parse_issue_comment_event,
    parse_pr_review_comment_event,
    parse_pr_review_event,
    parse_pull_request_event,
    parse_push_event,
)
from checkcord.adapters.code.providers.github.schemas import (
    GitHubIssueCommentPayload,
    GitHubPRPayload,
    GitHubPRReviewCommentPayload,
    GitHubPRReviewPayload,
    GitHubPushPayload,
)
from checkcord.api import rate_limit
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core.config import get_settings
from checkcord.core.crypto import decrypt_secret
from checkcord.models.pr_merge import PRMergedPayload
from checkcord.services import scope
from checkcord.services.connections import connection_key
from checkcord.storage.models import GithubInstallation

logger = structlog.get_logger(__name__).bind(component="github_webhook")

router = APIRouter()

# Collaborator changes and visibility changes (privatized, publicized).
AUDIENCE_EVENTS = ("member", "repository")


async def _refresh_repository_audience(
    db_engine: AsyncEngine,
    tenant_id: UUID,
    repo_name: str,
    pat: str,
    redis: Any,
    event_type: str,
) -> Response:
    from checkcord.adapters.code.providers.github.parser import read_github_repository
    from checkcord.adapters.code.providers.github.rest_client import GitHubRestClient
    from checkcord.services import audiences

    client = GitHubRestClient(pat)
    outcome = await audiences.refresh(
        db_engine,
        tenant_id,
        audiences.GITHUB,
        repo_name,
        trigger=f"github:{event_type}",
        reader=lambda: read_github_repository(client, repo_name),
        redis=redis,
    )
    status = "audience_refreshed" if outcome.error is None else "audience_unreadable"
    return Response(
        content=json.dumps({"status": status}), media_type="application/json", status_code=200
    )


async def get_db_engine(request: Request) -> AsyncEngine:
    ctx = getattr(request.app.state, "ctx", None)
    if ctx and hasattr(ctx, "db_engine") and ctx.db_engine:
        from typing import cast

        return cast(AsyncEngine, ctx.db_engine)
    engine = getattr(request.app.state, "db_engine", None)
    if not engine:
        settings = get_settings()
        engine = create_async_engine(settings.database_url)
        request.app.state.db_engine = engine
    return engine


@router.post("/api/v1/webhooks/github", status_code=202)
async def github_webhook(
    request: Request,
    x_github_event: str = Header(...),
    x_hub_signature_256: str = Header(...),
) -> Response:
    """FastAPI endpoint to receive and process GitHub webhooks."""
    body_bytes = await request.body()
    try:
        body_json = json.loads(body_bytes)
    except Exception as e:
        logger.error("malformed_json_payload", error=str(e))
        raise HTTPException(status_code=400, detail="Malformed JSON payload") from e

    repo_info = body_json.get("repository", {})
    repo_name = repo_info.get("full_name")

    if not repo_name:
        if x_github_event == "ping":
            return Response(
                content=json.dumps({"status": "ping_ok"}),
                media_type="application/json",
                status_code=200,
            )
        logger.warning("missing_repository_info")
        raise HTTPException(status_code=400, detail="Missing repository information")

    # Lookup GithubInstallation globally (bypassing RLS since tenant is unknown)
    db_engine = await get_db_engine(request)
    async with db_engine.connect() as conn:
        from sqlalchemy import text

        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        stmt = select(GithubInstallation).where(GithubInstallation.repo_identifier == repo_name)
        res = await conn.execute(stmt)
        installation = res.mappings().fetchone()

    if not installation:
        logger.warning("installation_not_found", repo_name=repo_name)
        raise HTTPException(status_code=401, detail="Unauthorized: Repository not installed")

    tenant_id: UUID = installation["tenant_id"]
    webhook_secret = decrypt_secret(installation["webhook_secret"])
    pat = decrypt_secret(installation["personal_access_token"])

    # Validate signature
    expected = (
        "sha256=" + hmac.new(webhook_secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    )

    if not hmac.compare_digest(expected, x_hub_signature_256):
        logger.warning("invalid_webhook_signature", repo_name=repo_name)
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid signature")

    await rate_limit.webhook(request, connection_key("github", repo_name))

    if x_github_event == "ping":
        return Response(
            content=json.dumps({"status": "ok"}), media_type="application/json", status_code=200
        )

    ctx = getattr(request.app.state, "ctx", None)
    if x_github_event in AUDIENCE_EVENTS:
        return await _refresh_repository_audience(
            db_engine, tenant_id, repo_name, pat, getattr(ctx, "redis_client", None), x_github_event
        )

    if not await scope.allows(db_engine, tenant_id, connection_key("github", repo_name), repo_name):
        return Response(
            content=json.dumps({"status": "out_of_scope"}),
            media_type="application/json",
            status_code=200,
        )

    if not ctx or not ctx.redis_client:
        raise HTTPException(status_code=500, detail="Redis client not initialized")
    if x_github_event == "workflow_run":
        return await handle_workflow_run(
            body_json, repo_name, tenant_id, pat, db_engine, ctx.redis_client
        )
    bus = RedisStreamsEventBus(ctx.redis_client)

    events_to_publish = []

    try:
        if x_github_event == "pull_request":
            pr_payload = GitHubPRPayload.model_validate(body_json)
            event = await parse_pull_request_event(
                pr_payload, db_engine, ctx.redis_client, tenant_id, pat
            )
            if event:
                events_to_publish.append(event)

            if pr_payload.action in ("opened", "synchronize"):
                sha = pr_payload.pull_request.head.sha or ""
                repo_full_name = pr_payload.repository.full_name
                owner, repo_short = repo_full_name.split("/", 1)

                # Set pending commit status
                try:
                    from checkcord.adapters.code.providers.github.rest_client import (
                        GitHubRestClient,
                    )
                    from checkcord.workers.pr_status_reporter import CHECKING

                    client = GitHubRestClient(pat)
                    await client.create_commit_status(
                        owner=owner,
                        repo=repo_short,
                        sha=sha,
                        state="pending",
                        description=CHECKING,
                    )
                except Exception as status_err:
                    logger.error("failed_to_set_pending_status", sha=sha, error=str(status_err))

                # Record state and add fallback task to ZSET
                try:
                    import time

                    from checkcord.workers.pr_status_reporter import CHECK_DEADLINE_SECONDS

                    await ctx.redis_client.set(f"ci:pr_status:{sha}", "pending")

                    fallback_val = json.dumps(
                        {
                            "owner": owner,
                            "repo": repo_short,
                            "sha": sha,
                            "pr_number": pr_payload.number,
                            "tenant_id": str(tenant_id),
                        }
                    )
                    await ctx.redis_client.zadd(
                        "ci.pr.fallback_zset",
                        {fallback_val: time.time() + CHECK_DEADLINE_SECONDS},
                    )
                except Exception as redis_fallback_err:
                    logger.error(
                        "failed_to_register_fallback", sha=sha, error=str(redis_fallback_err)
                    )

                ci_payload = {
                    "repository_full_name": repo_full_name,
                    "pull_request_number": str(pr_payload.number),
                    "head_sha": sha,
                    "head_ref": pr_payload.pull_request.head.ref,
                    "tenant_id": str(tenant_id),
                    "action": pr_payload.action,
                }
                try:
                    await ctx.redis_client.xadd("ci.pr.evaluate", fields=ci_payload)
                    logger.info(
                        "github_pr_ci_event_published",
                        repo=pr_payload.repository.full_name,
                        pr=pr_payload.number,
                        sha=pr_payload.pull_request.head.sha,
                        action=pr_payload.action,
                    )
                except Exception as redis_err:
                    logger.exception(
                        "failed_to_publish_ci_pr_event",
                        repo=pr_payload.repository.full_name,
                        pr=pr_payload.number,
                        error=str(redis_err),
                    )
                    raise HTTPException(
                        status_code=503,
                        detail="Redis stream unavailable",
                    ) from redis_err

            elif pr_payload.action == "closed" and pr_payload.pull_request.merged:
                # The AST writer fires only on an actual merge, never on
                # opened/synchronize, hence a separate branch from the one above.
                merge_sha = (
                    pr_payload.pull_request.merge_commit_sha or pr_payload.pull_request.head.sha
                )
                merged_payload = PRMergedPayload(
                    tenant_id=tenant_id,
                    repository_full_name=pr_payload.repository.full_name,
                    pull_request_number=pr_payload.number,
                    merge_commit_sha=merge_sha,
                )
                try:
                    await bus.publish("ci.pr.merged", merged_payload)
                    logger.info(
                        "github_pr_merged_event_published",
                        repo=pr_payload.repository.full_name,
                        pr=pr_payload.number,
                        merge_sha=merge_sha,
                    )
                except Exception as redis_err:
                    logger.exception(
                        "failed_to_publish_ci_pr_merged_event",
                        repo=pr_payload.repository.full_name,
                        pr=pr_payload.number,
                        error=str(redis_err),
                    )
                    raise HTTPException(
                        status_code=503,
                        detail="Redis stream unavailable",
                    ) from redis_err

        elif x_github_event == "pull_request_review":
            review_payload = GitHubPRReviewPayload.model_validate(body_json)
            event = await parse_pr_review_event(
                review_payload, db_engine, ctx.redis_client, tenant_id, pat
            )
            if event:
                events_to_publish.append(event)

        elif x_github_event == "pull_request_review_comment":
            comment_payload = GitHubPRReviewCommentPayload.model_validate(body_json)
            event = await parse_pr_review_comment_event(
                comment_payload, db_engine, ctx.redis_client, tenant_id, pat
            )
            if event:
                events_to_publish.append(event)

        elif x_github_event == "issue_comment":
            issue_payload = GitHubIssueCommentPayload.model_validate(body_json)
            event = await parse_issue_comment_event(
                issue_payload, db_engine, ctx.redis_client, tenant_id, pat
            )
            if event:
                events_to_publish.append(event)

        elif x_github_event == "push":
            push_payload = GitHubPushPayload.model_validate(body_json)
            events_to_publish = await parse_push_event(
                push_payload, db_engine, ctx.redis_client, tenant_id, pat
            )

        else:
            logger.info("ignored_github_event", event_type=x_github_event)
            return Response(
                content=json.dumps({"status": "ignored"}),
                media_type="application/json",
                status_code=200,
            )

    except HTTPException as http_err:
        raise http_err
    except Exception as parse_err:
        logger.exception("failed_to_parse_github_event", error=str(parse_err))
        # Return 202 Accepted to prevent GitHub from endlessly retrying but log the issue
        return Response(
            content=json.dumps({"status": "error_parsed", "details": str(parse_err)}),
            media_type="application/json",
            status_code=202,
        )

    # Publish events
    published_ids = []
    for event in events_to_publish:
        try:
            msg_id = await bus.publish(topic="events.raw.code", event=event)
            published_ids.append(str(event.event_id))
            logger.info("github_event_published", event_id=str(event.event_id), msg_id=msg_id)
        except Exception as pub_err:
            logger.exception(
                "failed_to_publish_github_event", event_id=str(event.event_id), error=str(pub_err)
            )

    return Response(
        content=json.dumps({"status": "processed", "event_ids": published_ids}),
        media_type="application/json",
        status_code=202,
    )
