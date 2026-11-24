"""The Slack Bolt app and its Socket Mode handler."""

import asyncio

import structlog
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.app.async_app import AsyncApp
from slack_bolt.oauth.async_oauth_settings import AsyncOAuthSettings
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.chat.providers.slack.oauth import PostgresInstallationStore, authorize_url
from checkcord.core.config import Settings

logger = structlog.get_logger(__name__)

_TASKS: set[asyncio.Task[None]] = set()


def init_slack_app(settings: Settings, engine: AsyncEngine) -> AsyncApp:
    """Initialize the Bolt AsyncApp with PostgresInstallationStore."""
    logger.info("slack.app.initializing")
    installation_store = PostgresInstallationStore(engine)
    oauth_settings = None
    if settings.slack_client_id and settings.slack_client_secret:
        oauth_settings = AsyncOAuthSettings(
            client_id=settings.slack_client_id,
            client_secret=settings.slack_client_secret,
            installation_store=installation_store,
            scopes=settings.slack_bot_scopes,
            authorization_url=authorize_url(settings),
        )
    return AsyncApp(
        client=AsyncWebClient(base_url=settings.slack_api_url),
        signing_secret=settings.slack_signing_secret,
        installation_store=installation_store,
        oauth_settings=oauth_settings,
    )


def get_slack_app(db_engine: AsyncEngine) -> AsyncApp:
    """Get initialized Slack Bolt AsyncApp using database-backed PostgresInstallationStore."""
    from checkcord.core.config import get_settings

    settings = get_settings()
    return init_slack_app(settings, db_engine)


async def start_slack_socket_handler(
    app: AsyncApp,
    app_token: str,
) -> AsyncSocketModeHandler:
    """Start the AsyncSocketModeHandler as an async background task.

    Returns the running handler instance.
    """
    logger.info("slack.socket_mode.starting")
    handler = AsyncSocketModeHandler(app, app_token)

    task = asyncio.get_running_loop().create_task(handler.start_async())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return handler
