from uuid import UUID, uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core.crypto import encrypt_secret
from checkcord.models.repo_backfill import RepoBackfillRequestPayload
from checkcord.schemas.repo_connect import RepoConnectRequest
from checkcord.storage import audit
from checkcord.storage.models import GithubInstallation
from checkcord.storage.postgres import app_bypass_rls_context

logger = structlog.get_logger(__name__).bind(component="repo_connect")


class RepoOwnedByOtherTenantError(Exception):
    pass


class BackfillPublishError(Exception):
    pass


async def upsert_github_installation(
    engine: AsyncEngine, tenant_id: UUID, payload: RepoConnectRequest, actor: audit.Actor
) -> None:
    async with app_bypass_rls_context(engine) as conn:
        existing = await conn.execute(
            select(GithubInstallation.tenant_id).where(
                GithubInstallation.repo_identifier == payload.repo_identifier
            )
        )
        existing_tenant_id = existing.scalar_one_or_none()
        if existing_tenant_id is not None and existing_tenant_id != tenant_id:
            logger.warning(
                "repo_connect_cross_tenant_conflict",
                repo_identifier=payload.repo_identifier,
                requesting_tenant_id=str(tenant_id),
                owning_tenant_id=str(existing_tenant_id),
            )
            raise RepoOwnedByOtherTenantError(payload.repo_identifier)

        pat = encrypt_secret(payload.personal_access_token)
        webhook_secret = encrypt_secret(payload.webhook_secret)
        stmt = (
            pg_insert(GithubInstallation)
            .values(
                id=uuid4(),
                repo_identifier=payload.repo_identifier,
                tenant_id=tenant_id,
                personal_access_token=pat,
                webhook_secret=webhook_secret,
            )
            .on_conflict_do_update(
                index_elements=["repo_identifier"],
                set_={"personal_access_token": pat, "webhook_secret": webhook_secret},
            )
        )
        await conn.execute(stmt)
        await audit.record(
            conn,
            tenant_id,
            actor,
            "connection.added" if existing_tenant_id is None else "connection.updated",
            params={"provider": "github", "identifier": payload.repo_identifier},
        )


async def request_backfill(
    bus: RedisStreamsEventBus, tenant_id: UUID, repo_identifier: str
) -> None:
    backfill_payload = RepoBackfillRequestPayload(
        tenant_id=tenant_id, repo_identifier=repo_identifier
    )
    try:
        await bus.publish("repo.backfill.requested", backfill_payload)
    except Exception as redis_err:
        logger.exception(
            "failed_to_publish_repo_backfill_requested",
            repo_identifier=repo_identifier,
            error=str(redis_err),
        )
        raise BackfillPublishError(repo_identifier) from redis_err

    logger.info(
        "repo_connected_backfill_requested",
        repo_identifier=repo_identifier,
        tenant_id=str(tenant_id),
    )
