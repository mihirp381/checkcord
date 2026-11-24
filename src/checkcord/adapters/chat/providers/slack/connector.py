import asyncio
from collections.abc import Mapping
from typing import Any

import structlog

from checkcord.adapters.chat.providers.slack import directory_sync
from checkcord.adapters.chat.providers.slack.oauth import router, seal_installation_info
from checkcord.connectors.base import Connector, Listener, ListenerContext, OAuthApp, Poll
from checkcord.core.config import SLACK_BOT_EVENTS, Settings
from checkcord.core.crypto import decrypt_secret
from checkcord.storage.models import SlackInstallation

logger = structlog.get_logger(__name__)


async def _socket_mode(ctx: ListenerContext) -> None:
    if not ctx.settings.slack_app_token:
        return
    try:
        from checkcord.adapters.chat.providers.slack.app import (
            get_slack_app,
            start_slack_socket_handler,
        )
        from checkcord.adapters.chat.providers.slack.listeners import register_listeners

        slack_app = get_slack_app(ctx.engine)
        register_listeners(slack_app, ctx.bus, ctx.engine)
        handler = await start_slack_socket_handler(slack_app, ctx.settings.slack_app_token)
        logger.info("slack_socket_mode_handler_started")
    except Exception as e:
        logger.error("failed_to_start_slack_socket_mode_handler", error=str(e))
        return
    try:
        await asyncio.Event().wait()
    finally:
        logger.info("stopping_slack_socket_mode_handler")
        try:
            await handler.close_async()
            logger.info("slack_socket_mode_handler_stopped")
        except Exception as e:
            logger.error("failed_to_stop_slack_socket_mode_handler", error=str(e))


async def _revoke(row: Mapping[str, Any], settings: Settings) -> None:
    from slack_sdk.web.async_client import AsyncWebClient

    try:
        result = await AsyncWebClient(
            token=decrypt_secret(row["bot_token"]), base_url=settings.slack_api_url
        ).apps_uninstall(
            client_id=settings.slack_client_id, client_secret=settings.slack_client_secret
        )
        if not result.get("ok"):
            logger.warning("slack_apps_uninstall_not_ok", error=result.get("error"))
    except Exception as err:
        logger.warning("slack_apps_uninstall_failed", error=str(err))


def _notes(settings: Settings) -> list[str]:
    return [
        f"Bot token scopes: {', '.join(settings.slack_bot_scopes)}",
        f"Bot events:       {', '.join(SLACK_BOT_EVENTS)}",
    ]


CONNECTORS = (
    Connector(
        name="slack",
        title="Slack",
        ingestion="Socket Mode events",
        status="beta",
        scope="Slack channel ids",
        audience_platform="slack",
        audience_from="source",
        table=SlackInstallation,
        identifier="team_id",
        secret_columns=("bot_token",),
        sealed_json=(("installation_info", seal_installation_info),),
        routers=((router, "install"),),
        poll=Poll(
            "sync_slack_directory",
            directory_sync.run_scheduled,
            lambda s: s.directory_sync_interval_minutes,
            publishes=False,
        ),
        listener=Listener(_socket_mode, "api"),
        oauth=OAuthApp("slack_client_id", "slack_redirect_uri", _notes),
        disconnect=_revoke,
    ),
)
