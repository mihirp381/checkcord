"""Keep each Slack channel's, Teams conversation's and GitHub repository's audience current."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.core.config import get_settings
from checkcord.models.common import AccessControl
from checkcord.services import accounts, connection_audiences
from checkcord.storage import audiences as store
from checkcord.storage import audit
from checkcord.storage.postgres import list_tenant_ids, tenant_context

logger = structlog.get_logger(__name__).bind(component="audiences")

SLACK, MS_TEAMS, GITHUB = "slack", "ms_teams", "github"
AUDIENCE_TOPIC = "contexts.audience_changed"


def platforms() -> list[str]:
    """Platforms whose audiences are read again from the provider."""
    from checkcord import connectors

    return [c.audience_platform for c in connectors.connectors() if c.audience_platform]


class ReadFailed(Exception):
    """The provider could not say who can read the container."""


@dataclass(frozen=True)
class Reading:
    is_public: bool
    native_ids: tuple[str, ...] = ()
    # Reads the accounts of members Checkcord has not seen yet.
    lookup: accounts.Lookup | None = field(default=None, compare=False)


Reader = Callable[[], Awaitable[Reading]]


@dataclass(frozen=True)
class Outcome:
    platform: str
    container: str
    applied: store.Applied
    error: str | None = None


def _windows() -> tuple[timedelta, timedelta]:
    settings = get_settings()
    return (
        timedelta(minutes=settings.audience_refresh_interval_minutes),
        timedelta(hours=settings.audience_refresh_grace_hours),
    )


async def _audience(
    engine: AsyncEngine, tenant_id: UUID, platform: str, reading: Reading
) -> tuple[store.Audience, list[str]]:
    """The audience, and the members not matched to a person, who are left out."""
    if reading.is_public:
        return store.Audience(True), []
    matched, unmatched = await accounts.match(
        engine, tenant_id, platform, reading.native_ids, reading.lookup
    )
    return store.Audience.of(False, matched.values()), unmatched


async def _one_installation(
    engine: AsyncEngine, tenant_id: UUID, model: Any, column: Any, value: str | None
) -> Any:
    async with tenant_context(engine, tenant_id) as conn:
        stmt = select(model).where(model.tenant_id == tenant_id)
        if value:
            stmt = stmt.where(column == value)
        rows = (await conn.execute(stmt)).mappings().all()
    if len(rows) != 1:
        raise ReadFailed("connection removed" if not rows else "connection is ambiguous")
    return rows[0]


async def reader_for(
    engine: AsyncEngine,
    tenant_id: UUID,
    platform: str,
    container: str,
    details: Mapping[str, Any] | None = None,
) -> Reader:
    """A reader that uses the tenant's stored connection for the container's platform."""
    from checkcord.core.crypto import decrypt_secret
    from checkcord.storage.models import MSTeamsInstallation, SlackInstallation

    details = details or {}
    if platform == SLACK:
        from slack_sdk.web.async_client import AsyncWebClient

        from checkcord.adapters.chat.providers.slack.audience import read_slack_channel
        from checkcord.core import http_retry

        row = await _one_installation(
            engine, tenant_id, SlackInstallation, SlackInstallation.team_id, details.get("team_id")
        )
        client = http_retry.bounded_slack(
            AsyncWebClient(
                token=decrypt_secret(row["bot_token"]), base_url=get_settings().slack_api_url
            )
        )
        return lambda: read_slack_channel(client, container)
    if platform == MS_TEAMS:
        from checkcord.adapters.chat.providers.msteams.audience import read_msteams_conversation

        row = await _one_installation(
            engine,
            tenant_id,
            MSTeamsInstallation,
            MSTeamsInstallation.ms_tenant_id,
            details.get("ms_tenant_id"),
        )
        return lambda: read_msteams_conversation(
            db_engine=engine,
            tenant_id=tenant_id,
            ms_tenant_id=row["ms_tenant_id"],
            conversation_type=details.get("conversation_type"),
            channel_id=container,
            team_id=details.get("team_id"),
        )
    if platform == GITHUB:
        from checkcord.adapters.code.providers.github.parser import read_github_repository
        from checkcord.adapters.code.providers.github.rest_client import GitHubRestClient
        from checkcord.services.github_tokens import installation_token

        token = await installation_token(engine, tenant_id, container)
        if not token:
            raise ReadFailed("connection removed")
        client = GitHubRestClient(token)
        return lambda: read_github_repository(client, container)
    raise ReadFailed(f"unsupported platform {platform!r}")


async def refresh(
    engine: AsyncEngine,
    tenant_id: UUID,
    platform: str,
    container: str,
    *,
    trigger: str,
    reader: Reader | None = None,
    details: Mapping[str, Any] | None = None,
    redis: Redis | None = None,
    actor: audit.Actor | None = None,
) -> Outcome:
    """Read the container's audience and apply it to its events and records."""
    settings = get_settings()
    _, grace = _windows()
    error = None
    target = None
    unmatched: list[str] = []
    try:
        if reader is None:
            reader = await reader_for(engine, tenant_id, platform, container, details)
        target, unmatched = await _audience(engine, tenant_id, platform, await reader())
    except Exception as err:
        error = f"{type(err).__name__}: {err}"[:300]
        logger.warning(
            "audiences.read_failed",
            tenant_id=str(tenant_id),
            platform=platform,
            container=container,
            error=error,
        )
    applied = await store.apply(
        engine,
        tenant_id,
        platform,
        container,
        target,
        now=datetime.now(UTC),
        grace=grace,
        batch_size=settings.audience_refresh_batch_size,
        error=error,
        details=details,
        unmatched=None if target is None else unmatched,
    )
    if applied.changed or applied.events or trigger != "ingest":
        await _record(engine, tenant_id, platform, container, trigger, applied, actor)
    # A repository whose readers change may no longer be allowed what its file quotes.
    repo_changed = (
        platform == GITHUB and applied.before is not None and applied.before != applied.after
    )
    if applied.narrowed_records or repo_changed:
        await rewrite_files(redis, tenant_id, platform, container, applied.narrowed_records)
    return Outcome(platform, container, applied, error)


async def _record(
    engine: AsyncEngine,
    tenant_id: UUID,
    platform: str,
    container: str,
    trigger: str,
    applied: store.Applied,
    actor: audit.Actor | None,
) -> None:
    before = applied.before or store.EMPTY
    after = applied.after
    params: dict[str, Any] = {
        "platform": platform,
        "container": container,
        "trigger": trigger,
        "public_before": before.is_public,
        "public_after": after.is_public,
        "members_before": len(before.member_ids),
        "members_after": len(after.member_ids),
        "members_added": len(after.member_ids - before.member_ids),
        "members_removed": len(before.member_ids - after.member_ids),
        "matched": len(after.member_ids),
        "unmatched": applied.unmatched,
        "events_updated": applied.events,
        "records_updated": applied.records,
        "records_narrowed": len(applied.narrowed_records),
    }
    async with tenant_context(engine, tenant_id) as conn:
        await audit.record(
            conn,
            tenant_id,
            actor or audit.Actor(channel=audit.API),
            "audience.refreshed",
            params=params,
            result_count=applied.records,
        )


async def rewrite_files(
    redis: Redis | None, tenant_id: UUID, platform: str, container: str, record_ids: list[UUID]
) -> None:
    """Ask the context compiler to regenerate the files of the tenant's connected repositories."""
    if redis is None:
        logger.warning("audiences.rewrite_skipped_no_redis", records=len(record_ids))
        return
    from checkcord.bus.redis_streams import RedisStreamsEventBus
    from checkcord.models.common import AuthorInfo
    from checkcord.models.enums import SourcePlatform, SourceType
    from checkcord.models.event import CanonicalEvent

    content = f"{len(record_ids)} records narrowed"
    event = CanonicalEvent(
        tenant_id=tenant_id,
        source_platform=SourcePlatform.CUSTOM,
        source_type=SourceType.CUSTOM_PAYLOAD,
        source_id=f"audience-changed:{platform}:{container}",
        happened_at=datetime.now(UTC),
        author=AuthorInfo(source_id="checkcord"),
        content_raw=content,
        content_clean=content,
        access_control=AccessControl(),
        metadata={
            "audience_container": container,
            "canonical_context_ids": [str(r) for r in record_ids],
        },
    )
    try:
        await RedisStreamsEventBus(redis).publish(AUDIENCE_TOPIC, event)
    except Exception as err:
        logger.warning("audiences.rewrite_request_failed", error=str(err))


async def current(
    engine: AsyncEngine,
    tenant_id: UUID,
    platform: str,
    container: str,
    *,
    reader: Reader,
    details: Mapping[str, Any] | None = None,
    is_public_hint: bool | None = None,
    redis: Redis | None = None,
) -> AccessControl:
    """The stored audience when fresh; otherwise read, stored and applied first.

    ``is_public_hint`` is what the incoming event says; a stored audience that
    disagrees with it is read again.
    """
    interval, grace = _windows()
    # Without a database (unit tests use mocks) the provider is read directly.
    if isinstance(engine, AsyncEngine):
        async with tenant_context(engine, tenant_id) as conn:
            row = await store.get(conn, tenant_id, platform, container)
        if (
            row
            and store.is_fresh(row, datetime.now(UTC), interval, grace)
            and is_public_hint in (None, row["is_public"])
        ):
            return store.Audience.from_row(row).access_control(platform, container)
        outcome = await refresh(
            engine,
            tenant_id,
            platform,
            container,
            trigger="ingest",
            reader=reader,
            details=details,
            redis=redis,
        )
        return outcome.applied.after.access_control(platform, container)
    try:
        audience, _ = await _audience(engine, tenant_id, platform, await reader())
    except Exception as err:
        logger.warning(
            "audiences.read_failed", platform=platform, container=container, error=str(err)
        )
        audience = store.EMPTY
    return audience.access_control(platform, container)


async def refresh_due(engine: AsyncEngine, redis: Redis | None = None) -> int:
    """Scheduled job: refresh every stored container not tried within half the interval."""
    interval, _ = _windows()
    cutoff = datetime.now(UTC) - interval / 2
    done = 0
    for tenant_id in await list_tenant_ids(engine):
        async with tenant_context(engine, tenant_id) as conn:
            rows = [
                r
                for r in await store.list_rows(conn, tenant_id)
                if r["attempted_at"] is None or r["attempted_at"] <= cutoff
            ]
        for row in rows:
            try:
                await refresh(
                    engine,
                    tenant_id,
                    row["platform"],
                    row["container"],
                    trigger="schedule",
                    details=row["details"],
                    redis=redis,
                )
                done += 1
            except Exception:
                logger.exception(
                    "audiences.refresh_failed",
                    tenant_id=str(tenant_id),
                    platform=row["platform"],
                    container=row["container"],
                )
        try:
            await connection_audiences.resolve_again(engine, tenant_id, redis)
        except Exception:
            logger.exception("audiences.connection_resolve_failed", tenant_id=str(tenant_id))
    logger.info("audiences.refreshed", containers=done)
    return done


async def refresh_stored(
    engine: AsyncEngine,
    tenant_id: UUID,
    platform: str,
    container: str,
    *,
    trigger: str,
    reader: Reader | None = None,
    details: Mapping[str, Any] | None = None,
    redis: Redis | None = None,
    actor: audit.Actor | None = None,
) -> Outcome | None:
    """``refresh`` for a container Checkcord already stores; None when it stores none."""
    async with tenant_context(engine, tenant_id) as conn:
        row = await store.get(conn, tenant_id, platform, container)
    if row is None:
        return None
    return await refresh(
        engine,
        tenant_id,
        platform,
        container,
        trigger=trigger,
        reader=reader,
        details={**row["details"], **(details or {})},
        redis=redis,
        actor=actor,
    )


async def list_stored(engine: AsyncEngine, tenant_id: UUID) -> list[dict[str, Any]]:
    async with tenant_context(engine, tenant_id) as conn:
        return await store.list_rows(conn, tenant_id)


async def refresh_selected(
    engine: AsyncEngine,
    tenant_id: UUID,
    platform: str | None,
    container: str | None,
    *,
    trigger: str,
    redis: Redis | None,
    actor: audit.Actor,
) -> list[Outcome]:
    """One container, or every stored one when ``platform`` and ``container`` are None."""
    rows = await list_stored(engine, tenant_id)
    if platform is not None or container is not None:
        rows = [r for r in rows if (r["platform"], r["container"]) == (platform, container)]
        if not rows:
            rows = [{"platform": platform, "container": container, "details": {}}]
    return [
        await refresh(
            engine,
            tenant_id,
            r["platform"],
            r["container"],
            trigger=trigger,
            details=r["details"],
            redis=redis,
            actor=actor,
        )
        for r in rows
    ]
