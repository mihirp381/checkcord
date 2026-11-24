from urllib.parse import quote, urlencode
from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from starlette.background import BackgroundTask

from checkcord.connectors import install_page
from checkcord.core.oauth_state import InvalidStateError, consume_state, read_state
from checkcord.storage import audit
from checkcord.storage.models import MSTeamsInstallation
from checkcord.storage.postgres import app_bypass_rls_context

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/msteams/install")
async def msteams_install(request: Request, state: str | None = None) -> RedirectResponse:
    """Redirect to Microsoft admin consent from a `checkcord connect teams` install link."""
    settings = request.app.state.ctx.settings
    client_id = settings.msteams_client_id
    redirect_uri = settings.msteams_redirect_uri

    if not client_id or not redirect_uri:
        logger.error("msteams.oauth.not_configured")
        raise HTTPException(
            status_code=500,
            detail="Microsoft Teams is not configured on this server.",
        )
    try:
        read_state(state, "teams")
    except InvalidStateError as err:
        raise HTTPException(status_code=400, detail=f"Invalid install link: {err}") from err

    query = urlencode(
        {"client_id": client_id, "redirect_uri": redirect_uri, "state": state}, quote_via=quote
    )
    return RedirectResponse(url=f"{settings.ms_login_url}/common/adminconsent?{query}")


@router.get("/msteams/oauth_redirect", response_class=HTMLResponse)
async def msteams_oauth_redirect(request: Request) -> HTMLResponse:
    """Microsoft's admin consent redirect; shows the install page."""
    ctx = request.app.state.ctx
    try:
        tenant_id = await _install(request)
    except HTTPException as err:
        return install_page.failed("Microsoft Teams", str(err.detail), err.status_code)
    from checkcord.adapters.chat.providers.msteams.directory_sync import after_install

    return install_page.connected(
        "Microsoft Teams",
        BackgroundTask(after_install, ctx.db_engine, tenant_id, ctx.redis_client),
    )


async def _install(request: Request) -> UUID:
    """Store the installation from Microsoft's admin consent redirect."""
    query_params = request.query_params
    ctx = request.app.state.ctx
    try:
        state = await consume_state(ctx.redis_client, query_params.get("state"), "teams")
    except InvalidStateError as err:
        logger.warning("msteams_oauth_state_rejected", reason=str(err))
        raise HTTPException(status_code=400, detail=f"Invalid install link: {err}") from err

    if query_params.get("error"):
        raise HTTPException(status_code=400, detail="Admin consent was not granted")
    ms_tenant_id = query_params.get("tenant")
    if not ms_tenant_id:
        logger.warning("msteams.oauth.no_tenant")
        raise HTTPException(
            status_code=400,
            detail="The redirect has no tenant.",
        )

    tenant_id: UUID = state.tenant_id
    async with app_bypass_rls_context(ctx.db_engine) as conn:
        owner = await conn.scalar(
            select(MSTeamsInstallation.tenant_id).where(
                MSTeamsInstallation.ms_tenant_id == ms_tenant_id
            )
        )
        if owner is not None and owner != tenant_id:
            logger.warning("msteams_tenant_owned_by_other_tenant", ms_tenant_id=ms_tenant_id)
            raise HTTPException(
                status_code=409, detail="This Microsoft tenant is connected to a different tenant"
            )

        auth_payload = {k: v for k, v in query_params.items() if k != "state"}
        stmt = pg_insert(MSTeamsInstallation).values(
            ms_tenant_id=ms_tenant_id,
            tenant_id=tenant_id,
            auth_payload=auth_payload,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[MSTeamsInstallation.ms_tenant_id],
            set_={
                "auth_payload": stmt.excluded.auth_payload,
                "updated_at": func.now(),
            },
        )
        await conn.execute(stmt)
        await audit.record(
            conn,
            tenant_id,
            audit.Actor(channel=audit.API),
            "connection.added",
            params={"provider": "teams", "identifier": ms_tenant_id, "via": "oauth"},
        )
    logger.info("msteams_installation_saved", ms_tenant_id=ms_tenant_id, tenant_id=str(tenant_id))

    return tenant_id
