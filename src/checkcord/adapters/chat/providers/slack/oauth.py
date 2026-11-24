import logging
from datetime import datetime
from typing import Any
from urllib.parse import urljoin
from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from slack_bolt.error import BoltError
from slack_bolt.oauth.async_oauth_flow import AsyncOAuthFlow
from slack_bolt.oauth.async_oauth_settings import AsyncOAuthSettings
from slack_bolt.request.async_request import AsyncBoltRequest
from slack_bolt.response import BoltResponse
from slack_sdk.oauth.installation_store import Bot, Installation
from slack_sdk.oauth.installation_store.async_installation_store import AsyncInstallationStore
from slack_sdk.oauth.state_store.async_state_store import AsyncOAuthStateStore
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.background import BackgroundTask

from checkcord.adapters.chat.core.tenancy import tenant_for_new_installation
from checkcord.connectors import install_page
from checkcord.core.config import Settings, get_settings
from checkcord.core.crypto import decrypt_secret, encrypt_secret, is_encrypted
from checkcord.core.oauth_state import (
    STATE_TTL_SECONDS,
    InvalidStateError,
    consume_state,
    read_state,
)
from checkcord.storage import audit
from checkcord.storage.models import SlackInstallation
from checkcord.storage.postgres import app_bypass_rls_context

logger = structlog.get_logger(__name__)

router = APIRouter()

_TOKEN_FIELDS = ("bot_token", "bot_refresh_token", "user_token", "user_refresh_token")


def _sealed_value(key: str, value: Any) -> Any:
    if key in _TOKEN_FIELDS and not is_encrypted(value):
        return encrypt_secret(value)
    # JSONB cannot hold datetimes; slack_sdk parses ISO strings back.
    return value.isoformat() if isinstance(value, datetime) else value


def seal_installation_info(info: dict[str, Any]) -> dict[str, Any]:
    return {k: _sealed_value(k, v) for k, v in info.items()}


def open_installation_info(info: dict[str, Any]) -> dict[str, Any]:
    return {k: decrypt_secret(v) if k in _TOKEN_FIELDS and v else v for k, v in info.items()}


class PostgresInstallationStore(AsyncInstallationStore):
    """Slack Bolt installation store backed by slack_installations."""

    def __init__(self, engine: AsyncEngine, settings: Settings | None = None):
        self.engine = engine
        self.settings = settings or get_settings()

    async def async_save(self, installation: Installation) -> None:
        team_id = installation.team_id
        if not team_id:
            logger.warning("slack.oauth.installation_no_team")
            return

        async with app_bypass_rls_context(self.engine) as conn:
            # Query existing tenant_id
            select_stmt = select(SlackInstallation.tenant_id).where(
                SlackInstallation.team_id == team_id
            )
            result = await conn.execute(select_stmt)
            existing_row = result.fetchone()

            if existing_row:
                tenant_id = existing_row[0]
            else:
                tenant_id = tenant_for_new_installation(self.settings)
                # Log tenant_auto_provisioned
                logger.info(
                    "tenant_auto_provisioned",
                    team_id=team_id,
                    tenant_id=str(tenant_id),
                    platform="slack",
                )

            # Perform SQL insert/upsert using pg_insert into slack_installations table
            stmt = pg_insert(SlackInstallation).values(
                team_id=team_id,
                tenant_id=tenant_id,
                bot_token=encrypt_secret(installation.bot_token),
                installation_info=seal_installation_info(installation.to_dict()),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[SlackInstallation.team_id],
                set_={
                    "bot_token": stmt.excluded.bot_token,
                    "installation_info": stmt.excluded.installation_info,
                    "updated_at": func.now(),
                },
            )
            await conn.execute(stmt)

    async def save_for_tenant(self, installation: Installation, tenant_id: UUID) -> None:
        team_id = installation.team_id
        if not team_id:
            raise BoltError("Slack installation has no team_id")
        async with app_bypass_rls_context(self.engine) as conn:
            owner = await conn.scalar(
                select(SlackInstallation.tenant_id).where(SlackInstallation.team_id == team_id)
            )
            if owner is not None and owner != tenant_id:
                logger.warning("slack_team_owned_by_other_tenant", team_id=team_id)
                raise BoltError("Slack workspace is connected to a different tenant")
            stmt = pg_insert(SlackInstallation).values(
                team_id=team_id,
                tenant_id=tenant_id,
                bot_token=encrypt_secret(installation.bot_token),
                installation_info=seal_installation_info(installation.to_dict()),
            )
            await conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=[SlackInstallation.team_id],
                    set_={
                        "bot_token": stmt.excluded.bot_token,
                        "installation_info": stmt.excluded.installation_info,
                        "updated_at": func.now(),
                    },
                )
            )
            await audit.record(
                conn,
                tenant_id,
                audit.Actor(channel=audit.API),
                "connection.added",
                params={"provider": "slack", "identifier": team_id, "via": "oauth"},
            )
        logger.info("slack_installation_saved", team_id=team_id, tenant_id=str(tenant_id))

    async def async_save_bot(self, bot: Bot) -> None:
        team_id = bot.team_id
        if not team_id:
            logger.warning("slack.oauth.bot_no_team")
            return

        async with app_bypass_rls_context(self.engine) as conn:
            select_stmt = select(SlackInstallation.tenant_id).where(
                SlackInstallation.team_id == team_id
            )
            result = await conn.execute(select_stmt)
            existing_row = result.fetchone()

            if existing_row:
                tenant_id = existing_row[0]
            else:
                tenant_id = tenant_for_new_installation(self.settings)
                logger.info(
                    "tenant_auto_provisioned",
                    team_id=team_id,
                    tenant_id=str(tenant_id),
                    platform="slack",
                )

            stmt = pg_insert(SlackInstallation).values(
                team_id=team_id,
                tenant_id=tenant_id,
                bot_token=encrypt_secret(bot.bot_token),
                installation_info=seal_installation_info(bot.to_dict()),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[SlackInstallation.team_id],
                set_={
                    "bot_token": stmt.excluded.bot_token,
                    "installation_info": stmt.excluded.installation_info,
                    "updated_at": func.now(),
                },
            )
            await conn.execute(stmt)

    async def async_find_bot(
        self,
        *,
        enterprise_id: str | None,
        team_id: str | None,
        is_enterprise_install: bool | None = False,
    ) -> Bot | None:
        if not team_id:
            return None

        async with app_bypass_rls_context(self.engine) as conn:
            select_stmt = select(SlackInstallation.installation_info).where(
                SlackInstallation.team_id == team_id
            )
            result = await conn.execute(select_stmt)
            row = result.fetchone()
            if not row:
                return None
            info = open_installation_info(row[0])
            if "user_id" in info:
                return Installation(**info).to_bot()
            return Bot(**info)

    async def async_find_installation(
        self,
        *,
        enterprise_id: str | None,
        team_id: str | None,
        user_id: str | None = None,
        is_enterprise_install: bool | None = False,
    ) -> Installation | None:
        if not team_id:
            return None

        async with app_bypass_rls_context(self.engine) as conn:
            select_stmt = select(SlackInstallation.installation_info).where(
                SlackInstallation.team_id == team_id
            )
            result = await conn.execute(select_stmt)
            row = result.fetchone()
            if not row:
                return None
            info = open_installation_info(row[0])
            if "user_id" in info:
                return Installation(**info)
            user_id = info.get("user_id") or info.get("bot_user_id") or "unknown_user"
            info_copy = dict(info)
            info_copy["user_id"] = user_id
            return Installation(**info_copy)

    async def async_delete_bot(self, *, enterprise_id: str | None, team_id: str | None) -> None:
        if not team_id:
            return
        async with app_bypass_rls_context(self.engine) as conn:
            stmt = delete(SlackInstallation).where(SlackInstallation.team_id == team_id)
            await conn.execute(stmt)

    async def async_delete_installation(
        self,
        *,
        enterprise_id: str | None,
        team_id: str | None,
        user_id: str | None = None,
    ) -> None:
        if not team_id:
            return
        async with app_bypass_rls_context(self.engine) as conn:
            stmt = delete(SlackInstallation).where(SlackInstallation.team_id == team_id)
            await conn.execute(stmt)

    async def async_delete_all(self, *, enterprise_id: str | None, team_id: str | None) -> None:
        if not team_id:
            return
        async with app_bypass_rls_context(self.engine) as conn:
            stmt = delete(SlackInstallation).where(SlackInstallation.team_id == team_id)
            await conn.execute(stmt)


def to_fastapi_response(bolt_resp: BoltResponse) -> Response:
    """Wrap a BoltResponse into a FastAPI Response object."""
    response = Response(
        content=bolt_resp.body,
        status_code=bolt_resp.status,
    )
    if bolt_resp.headers:
        for k, v in bolt_resp.headers.items():
            if isinstance(v, list):
                for val in v:
                    response.headers.append(k, val)
            else:
                response.headers.append(k, str(v))
    return response


class SignedStateStore(AsyncOAuthStateStore):
    """Consumes Checkcord-signed states once; issuing happens in ``checkcord connect slack``."""

    def __init__(self, redis: Any) -> None:
        self.redis = redis

    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger(__name__)

    async def async_issue(self, *args: Any, **kwargs: Any) -> str:
        raise BoltError("Slack install links are issued by `checkcord connect slack`")

    async def async_consume(self, state: str) -> bool:
        try:
            await consume_state(self.redis, state, "slack")
        except InvalidStateError as err:
            logger.warning("slack_oauth_state_rejected", reason=str(err))
            return False
        return True


def authorize_url(settings: Settings) -> str:
    return urljoin(settings.slack_api_url, "/oauth/v2/authorize")


class CheckcordOAuthFlow(AsyncOAuthFlow):
    async def issue_new_state(self, request: AsyncBoltRequest) -> str:
        return str(request.query["state"][0])

    async def store_installation(
        self, request: AsyncBoltRequest, installation: Installation
    ) -> None:
        state = read_state(request.query.get("state", [None])[0], "slack")
        store = self.settings.installation_store
        if not isinstance(store, PostgresInstallationStore):
            raise BoltError("unsupported installation store")
        await store.save_for_tenant(installation, state.tenant_id)


def get_oauth_flow(request: Request) -> AsyncOAuthFlow:
    """Retrieve or create the cached Slack AsyncOAuthFlow."""
    app = request.app
    if not hasattr(app.state, "slack_oauth_flow"):
        ctx = app.state.ctx
        settings = ctx.settings
        installation_store = PostgresInstallationStore(ctx.db_engine, settings)
        app.state.slack_oauth_flow = CheckcordOAuthFlow(
            client=AsyncWebClient(base_url=settings.slack_api_url),
            settings=AsyncOAuthSettings(
                client_id=settings.slack_client_id,
                client_secret=settings.slack_client_secret,
                redirect_uri=settings.slack_redirect_uri,
                installation_store=installation_store,
                state_validation_enabled=True,
                state_store=SignedStateStore(ctx.redis_client),
                state_expiration_seconds=STATE_TTL_SECONDS,
                scopes=settings.slack_bot_scopes,
                authorization_url=authorize_url(settings),
            ),
        )
    flow: AsyncOAuthFlow = app.state.slack_oauth_flow
    return flow


def _require_state(request: Request) -> None:
    try:
        read_state(request.query_params.get("state"), "slack")
    except InvalidStateError as err:
        raise HTTPException(status_code=400, detail=f"Invalid install link: {err}") from err


def _bolt_request(request: Request) -> AsyncBoltRequest:
    return AsyncBoltRequest(
        body="",
        query=dict(request.query_params),
        headers=dict(request.headers),
    )


@router.get("/slack/install")
async def slack_install(request: Request) -> Response:
    """Start the Slack OAuth flow from an install link minted by `checkcord connect slack`."""
    _require_state(request)
    bolt_resp = await get_oauth_flow(request).handle_installation(_bolt_request(request))
    return to_fastapi_response(bolt_resp)


@router.get("/slack/oauth_redirect", response_class=HTMLResponse)
async def slack_oauth_redirect(request: Request) -> Response:
    """Slack's OAuth redirect; shows the install page."""
    try:
        _require_state(request)
    except HTTPException as err:
        return install_page.failed("Slack", str(err.detail), err.status_code)
    bolt_resp = await get_oauth_flow(request).handle_callback(_bolt_request(request))
    if bolt_resp.status >= 400:
        return install_page.failed("Slack", "Slack was not connected.", bolt_resp.status)
    from checkcord.adapters.chat.providers.slack.directory_sync import after_install

    ctx = request.app.state.ctx
    return install_page.connected(
        "Slack",
        BackgroundTask(
            after_install,
            ctx.db_engine,
            read_state(request.query_params.get("state"), "slack").tenant_id,
            ctx.redis_client,
        ),
    )
