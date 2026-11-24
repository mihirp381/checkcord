"""SCIM 2.0 service at /scim/v2. Every request needs an admin key."""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from checkcord.api.deps import require_admin_key
from checkcord.scim import discovery, groups, users
from checkcord.scim.protocol import SCIMException


async def verify_scim_token(
    request: Request, authorization: str | None = Header(default=None)
) -> UUID:
    try:
        principal = await require_admin_key(request, authorization)
    except HTTPException as exc:
        raise SCIMException(exc.status_code, str(exc.detail)) from exc
    return principal.tenant_id


router = APIRouter(prefix="/scim/v2", dependencies=[Depends(verify_scim_token)])
router.include_router(discovery.router)
router.include_router(users.router)
router.include_router(groups.router)
