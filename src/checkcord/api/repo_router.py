"""Repo-connect endpoint for operators.

Upserts the `GithubInstallation` row and publishes
`repo.backfill.requested` in the same call.

Requires an admin key, since it stores repository credentials. `tenant_id` is
always resolved from the caller's own token, never taken from the request
body, so a tenant's token can only ever connect a repo to itself.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.api.deps import Principal, get_db_engine, require_admin_principal
from checkcord.api.schemas import ERRORS
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.schemas.repo_connect import RepoConnectRequest
from checkcord.services.repo_connect import (
    BackfillPublishError,
    RepoOwnedByOtherTenantError,
    request_backfill,
    upsert_github_installation,
)

router = APIRouter(responses=ERRORS)


@router.post(
    "/api/v1/admin/repos/connect",
    status_code=202,
    tags=["admin: connections"],
    summary="Connect a GitHub repository",
)
async def connect_repo(
    payload: RepoConnectRequest,
    request: Request,
    principal: Principal = Depends(require_admin_principal),
    db_engine: AsyncEngine = Depends(get_db_engine),
) -> dict[str, str]:
    """Register (or refresh) a GithubInstallation and trigger a full AST backfill."""
    tenant_id = principal.tenant_id
    try:
        await upsert_github_installation(db_engine, tenant_id, payload, principal.as_actor())
    except RepoOwnedByOtherTenantError as err:
        raise HTTPException(
            status_code=409, detail="Repository is already connected to a different tenant"
        ) from err

    ctx = getattr(request.app.state, "ctx", None)
    if not ctx or not ctx.redis_client:
        raise HTTPException(status_code=500, detail="Redis client not initialized")

    try:
        await request_backfill(
            RedisStreamsEventBus(ctx.redis_client), tenant_id, payload.repo_identifier
        )
    except BackfillPublishError as err:
        raise HTTPException(status_code=503, detail="Redis stream unavailable") from err

    return {"status": "connected", "repo_identifier": payload.repo_identifier}
