from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.code.providers.github.parser import (
    parse_issue_comment_event,
    parse_pr_review_event,
    parse_pull_request_event,
    parse_push_event,
)
from checkcord.adapters.code.providers.github.schemas import (
    GitHubIssueCommentPayload,
    GitHubPRPayload,
    GitHubPRReviewPayload,
    GitHubPushPayload,
)
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core import http_retry
from checkcord.core.config import get_settings
from checkcord.core.crypto import decrypt_secret
from checkcord.services import scope
from checkcord.services.connections import connection_key
from checkcord.storage.models import GithubInstallation

logger = structlog.get_logger(__name__).bind(component="github_poller")

PAGE_SIZE = 100
# The events API serves at most 300 events per repository.
MAX_PAGES = 3


def _seen(event: dict[str, Any], last_id: str | None, last_time: datetime) -> bool:
    event_id = str(event.get("id"))
    if last_id is not None and event_id.isdigit() and last_id.isdigit():
        return int(event_id) <= int(last_id)
    created = datetime.strptime(event["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    return event_id == last_id or created <= last_time


async def _new_events(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    repo_identifier: str,
    last_id: str | None,
    last_time: datetime,
) -> list[dict[str, Any]] | None:
    """Events after the checkpoint, oldest first; None when a page cannot be read."""
    found: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        response = await http_retry.send(
            "github",
            client,
            "GET",
            url,
            headers=headers,
            params={"per_page": PAGE_SIZE, "page": page},
        )
        if response.status_code != 200:
            logger.error(
                "failed_to_fetch_github_events",
                status_code=response.status_code,
                repo=repo_identifier,
                page=page,
                body=response.text,
            )
            return None
        batch = response.json()
        if not isinstance(batch, list):
            logger.error("invalid_events_response_format", repo=repo_identifier)
            return None
        batch = [e for e in batch if e.get("created_at")]
        fresh = [e for e in batch if not _seen(e, last_id, last_time)]
        found.extend(fresh)
        if len(fresh) < len(batch) or len(batch) < PAGE_SIZE:
            return list(reversed(found))
    logger.warning(
        "github_events_window_exceeded",
        repo=repo_identifier,
        detail=f"more than {PAGE_SIZE * MAX_PAGES} events since the last poll; "
        "older ones are only in the repository backfill",
    )
    return list(reversed(found))


async def poll_github_history(db_engine: AsyncEngine, bus: RedisStreamsEventBus) -> int:
    """Poll recent GitHub events for all registered installations; how many were published."""
    logger.info("github.poller.started")
    published = 0

    async with db_engine.connect() as conn:
        from sqlalchemy import text

        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        stmt = select(GithubInstallation)
        result = await conn.execute(stmt)
        installations = [dict(row) for row in result.mappings().fetchall()]

    for inst in installations:
        repo_identifier = inst["repo_identifier"]
        tenant_id = inst["tenant_id"]
        pat = decrypt_secret(inst["personal_access_token"])
        key = connection_key("github", repo_identifier)
        if not await scope.allows(db_engine, tenant_id, key, repo_identifier, count=False):
            continue

        owner, repo = repo_identifier.split("/")

        last_event_id_key = f"github:last_event_id:{repo_identifier}"
        last_event_time_key = f"github:last_event_time:{repo_identifier}"

        last_event_id = None
        last_event_time = None

        if bus.redis:
            try:
                last_event_id = await bus.redis.get(last_event_id_key)
                if last_event_id:
                    last_event_id = last_event_id.decode("utf-8")
                last_event_time_str = await bus.redis.get(last_event_time_key)
                if last_event_time_str:
                    last_event_time = datetime.fromisoformat(last_event_time_str.decode("utf-8"))
            except Exception as e:
                logger.warning("failed_to_fetch_polling_state_from_redis", error=str(e))

        # The first poll starts now; the repository backfill covers what came before.
        if not last_event_time:
            last_event_time = datetime.now(UTC).replace(tzinfo=None)

        logger.info(
            "github.poller.polling",
            repo=repo_identifier,
            last_event_id=last_event_id,
            last_event_time=last_event_time.isoformat(),
        )

        url = f"{get_settings().github_api_url}/repos/{owner}/{repo}/events"
        headers = {
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                events_to_process = await _new_events(
                    client, url, headers, repo_identifier, last_event_id, last_event_time
                )
                if events_to_process is None:
                    continue

                latest_processed_id = last_event_id
                latest_processed_time = last_event_time

                for event in events_to_process:
                    event_type = event.get("type")
                    payload = event.get("payload", {})
                    event_id = event.get("id")
                    created_at_str = event.get("created_at")
                    event_time = datetime.strptime(created_at_str, "%Y-%m-%dT%H:%M:%SZ")

                    # Add repository field to payload if missing (GitHub API repo format differs)
                    payload["repository"] = {
                        "full_name": repo_identifier,
                        "private": True,  # Fallback to private since we are using PAT
                    }

                    # Map payload structures to schemas and parse
                    events_to_publish = []
                    try:
                        if event_type == "PullRequestEvent":
                            pr_payload = GitHubPRPayload.model_validate(payload)
                            parsed = await parse_pull_request_event(
                                pr_payload, db_engine, bus.redis, tenant_id, pat
                            )
                            if parsed:
                                events_to_publish.append(parsed)

                        elif event_type == "PullRequestReviewEvent":
                            review_payload = GitHubPRReviewPayload.model_validate(payload)
                            parsed = await parse_pr_review_event(
                                review_payload, db_engine, bus.redis, tenant_id, pat
                            )
                            if parsed:
                                events_to_publish.append(parsed)

                        elif event_type == "IssueCommentEvent":
                            issue_payload = GitHubIssueCommentPayload.model_validate(payload)
                            parsed = await parse_issue_comment_event(
                                issue_payload, db_engine, bus.redis, tenant_id, pat
                            )
                            if parsed:
                                events_to_publish.append(parsed)

                        elif event_type == "PushEvent":
                            push_payload = GitHubPushPayload.model_validate(payload)
                            events_to_publish = await parse_push_event(
                                push_payload, db_engine, bus.redis, tenant_id, pat
                            )

                    except Exception as parse_err:
                        logger.warning(
                            "failed_to_parse_polled_event",
                            event_id=event_id,
                            event_type=event_type,
                            error=str(parse_err),
                        )
                        # Still update checkpoint so we don't get stuck on a bad event
                        latest_processed_id = event_id
                        latest_processed_time = event_time
                        continue

                    # events.raw.chat takes RawChatPayload only; polled PR, push and review
                    # events are CanonicalEvents and go to events.raw.code, as on the
                    # webhook path.
                    try:
                        for pe in events_to_publish:
                            msg_id = await bus.publish(topic="events.raw.code", event=pe)
                            published += 1
                            logger.info(
                                "polled_github_event_published",
                                event_id=str(pe.event_id),
                                msg_id=msg_id,
                            )
                    except Exception as e:
                        logger.error(
                            "failed_to_publish_polled_github_event",
                            github_event_id=event_id,
                            error=str(e),
                        )
                        break

                    latest_processed_id = event_id
                    latest_processed_time = event_time

                # Save checkpoint to Redis
                if bus.redis and latest_processed_id != last_event_id:
                    try:
                        await bus.redis.set(last_event_id_key, latest_processed_id)
                        await bus.redis.set(last_event_time_key, latest_processed_time.isoformat())
                        logger.info(
                            "updated_polling_checkpoint",
                            repo=repo_identifier,
                            last_event_id=latest_processed_id,
                            last_event_time=latest_processed_time.isoformat(),
                        )
                    except Exception as e:
                        logger.error("failed_to_save_polling_checkpoint", error=str(e))

        except Exception as e:
            logger.error("error_polling_github_history", repo=repo_identifier, error=str(e))
    return published
