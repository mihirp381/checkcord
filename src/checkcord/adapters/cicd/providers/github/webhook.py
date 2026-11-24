import hashlib
import hmac
import json
from typing import Any
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.adapters.cicd.providers.github.provider import GitHubCICDProvider
from checkcord.adapters.cicd.providers.github.schemas import GitHubWorkflowRunPayload
from checkcord.api import rate_limit
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core.config import get_settings
from checkcord.core.crypto import decrypt_secret
from checkcord.core.exceptions import GitHubAPIError
from checkcord.llm import usage
from checkcord.models.extractions import DiscourseSegment
from checkcord.pipeline.agents.specialists import extract_ci_failure
from checkcord.pipeline.redaction import redact_text
from checkcord.services import scope
from checkcord.services.ci_failure_linker import link_ci_failure_to_contexts
from checkcord.services.connections import connection_key
from checkcord.storage.models import GithubInstallation
from checkcord.storage.postgres import PostgresEventStore, tenant_context

logger = structlog.get_logger(__name__).bind(component="github_cicd_webhook")

router = APIRouter()

_cicd_provider = GitHubCICDProvider()


async def _fetch_prepare_failure_content(
    wr_payload: GitHubWorkflowRunPayload,
    repo_name: str,
    tenant_id: UUID,
    pat: str,
    event_id: UUID,
) -> tuple[str | None, list[str], str | None]:
    """Fetch failure content, redact it and run structured extraction.

    Best effort and never raises: a failure here means no enrichment, not a
    broken webhook response.

    Returns (redacted_content_text, extracted_summaries, first_extracted_file_path).
    """
    run = wr_payload.workflow_run
    usage.set_tenant(tenant_id)
    try:
        owner, repo = repo_name.split("/", 1)
        settings = get_settings()
        failure = await _cicd_provider.fetch_failure_content(
            owner,
            repo,
            run.head_sha,
            run.id,
            pat,
            max_log_lines=settings.ci_log_excerpt_max_lines,
        )

        if failure.source == "annotations":
            raw_text = "\n".join(
                f"{a.path or 'unknown file'}:{a.start_line or '?'}: {a.message}"
                for a in failure.annotations
            )
        elif failure.source == "job_log":
            raw_text = failure.log_excerpt or ""
        else:
            return None, [], None

        if not raw_text.strip():
            return None, [], None

        # Redact before this content reaches content_raw/content_clean.
        redacted_text = await redact_text(raw_text)

        segment = DiscourseSegment(
            parent_event_id=event_id,
            tenant_id=tenant_id,
            text_content=redacted_text,
        )
        extracted = await extract_ci_failure(segment)
        summaries = [f.summary for f in extracted if f.summary]
        first_file_path = next((f.file_path for f in extracted if f.file_path), None)

        return redacted_text, summaries, first_file_path
    except Exception as e:
        logger.exception(
            "cicd_webhook.failure_content_pipeline_failed", repo=repo_name, error=str(e)
        )
        return None, [], None


async def _refresh_workflow_definition(
    wr_payload: GitHubWorkflowRunPayload,
    repo_name: str,
    tenant_id: UUID,
    pat: str,
    db_engine: AsyncEngine,
) -> None:
    """Best-effort refresh -- never raises, so a failure here can never abort
    the surrounding webhook's actual CI-failure-event publish flow (this
    function's own errors are logged and swallowed, not propagated).
    """
    workflow_path = wr_payload.workflow_run.path
    if not workflow_path:
        return

    try:
        owner, repo = repo_name.split("/", 1)
        definition = await _cicd_provider.fetch_workflow_definition(
            owner, repo, workflow_path, wr_payload.workflow_run.head_sha, pat
        )
        if definition is None:
            return

        async with tenant_context(db_engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.upsert_workflow_definition(
                tenant_id=tenant_id,
                repo_identifier=repo_name,
                workflow_path=workflow_path,
                name=definition.name,
                jobs=[job.model_dump(mode="json") for job in definition.jobs],
                commit_sha=wr_payload.workflow_run.head_sha,
            )
    except GitHubAPIError as e:
        logger.warning(
            "workflow_definition_refresh_failed",
            repo=repo_name,
            workflow_path=workflow_path,
            error=str(e),
        )
    except Exception as e:
        logger.exception(
            "workflow_definition_refresh_unexpected_error",
            repo=repo_name,
            workflow_path=workflow_path,
            error=str(e),
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


@router.post("/api/v1/webhooks/cicd/github", status_code=202)
async def github_cicd_webhook(
    request: Request,
    x_github_event: str = Header(...),
    x_hub_signature_256: str = Header(...),
) -> Response:
    """FastAPI endpoint to receive and process GitHub Action CI/CD webhooks."""
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

    # Lookup GithubInstallation globally
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

    if x_github_event != "workflow_run":
        logger.info("ignored_github_cicd_event", event_type=x_github_event)
        return Response(
            content=json.dumps({"status": "ignored"}),
            media_type="application/json",
            status_code=200,
        )

    if not await scope.allows(db_engine, tenant_id, connection_key("github", repo_name), repo_name):
        return Response(
            content=json.dumps({"status": "out_of_scope"}),
            media_type="application/json",
            status_code=200,
        )

    ctx = getattr(request.app.state, "ctx", None)
    if not ctx or not ctx.redis_client:
        raise HTTPException(status_code=500, detail="Redis client not initialized")
    return await handle_workflow_run(
        body_json, repo_name, tenant_id, pat, db_engine, ctx.redis_client
    )


async def handle_workflow_run(
    body_json: dict[str, Any],
    repo_name: str,
    tenant_id: UUID,
    pat: str,
    db_engine: AsyncEngine,
    redis_client: Any,
) -> Response:
    """Turn a verified ``workflow_run`` delivery into one CI event for either webhook URL."""
    bus = RedisStreamsEventBus(redis_client)

    try:
        wr_payload = GitHubWorkflowRunPayload.model_validate(body_json)

        # Refresh workflow_definitions on every workflow_run delivery for this
        # ref, pass or fail. Best effort: a failure here must not block the
        # failure-event publish below.
        await _refresh_workflow_definition(wr_payload, repo_name, tenant_id, pat, db_engine)

        run = wr_payload.workflow_run
        failure_content: str | None = None
        extracted_summaries: list[str] = []
        extracted_file_path: str | None = None
        pre_generated_event_id = uuid4()

        if run.status == "completed" and run.conclusion == "failure":
            # Fetch failure content, redact it and extract structured signal.
            # Uses the event_id the event below will carry, so log lines and
            # the published event correlate.
            (
                failure_content,
                extracted_summaries,
                extracted_file_path,
            ) = await _fetch_prepare_failure_content(
                wr_payload, repo_name, tenant_id, pat, pre_generated_event_id
            )

        event = await _cicd_provider.parse_run_event(
            wr_payload,
            db_engine,
            redis_client,
            tenant_id,
            pat,
            failure_content=failure_content,
            extracted_failure_summaries=extracted_summaries,
            event_id=pre_generated_event_id,
        )
    except Exception as parse_err:
        logger.exception("failed_to_parse_github_cicd_event", error=str(parse_err))
        return Response(
            content=json.dumps({"status": "error_parsed", "details": str(parse_err)}),
            media_type="application/json",
            status_code=202,
        )

    if not event:
        return Response(
            content=json.dumps({"status": "ignored_run"}),
            media_type="application/json",
            status_code=202,
        )

    try:
        msg_id = await bus.publish(topic="events.raw.monitoring", event=event)
        logger.info("github_cicd_event_published", event_id=str(event.event_id), msg_id=msg_id)
    except Exception as pub_err:
        logger.exception(
            "failed_to_publish_github_cicd_event", event_id=str(event.event_id), error=str(pub_err)
        )
        raise HTTPException(status_code=500, detail="Failed to publish to stream") from pub_err

    # Link the failure to code at file level after a successful publish.
    # Never raises: the delivery has already been accepted.
    try:
        async with tenant_context(db_engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await link_ci_failure_to_contexts(
                tenant_id=tenant_id,
                repo_identifier=repo_name,
                commit_sha=wr_payload.workflow_run.head_sha,
                ci_event_id=event.event_id,
                pat=pat,
                store=store,
                extracted_file_path=extracted_file_path,
            )
    except Exception as link_err:
        logger.exception(
            "ci_failure_linking_failed", event_id=str(event.event_id), error=str(link_err)
        )

    return Response(
        content=json.dumps({"status": "processed", "event_id": str(event.event_id)}),
        media_type="application/json",
        status_code=202,
    )
