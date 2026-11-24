import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import redis.asyncio
import uvloop
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.core.config import get_settings
from checkcord.core.logging import get_logger
from checkcord.storage.postgres import create_checkcord_engine

logger = get_logger(__name__)


class AppContext:
    """Database engines, Redis and settings shared by the app."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.db_engine: AsyncEngine | None = None
        self.redis_client: Redis | None = None


@asynccontextmanager
async def app_lifecycle() -> AsyncGenerator[AppContext, None]:
    """
    Manages the application lifecycle.

    Sets up uvloop, initializes connections, yields the context,
    and gracefully tears down connections upon exit.
    """
    logger.info("lifecycle.starting")

    # Set uvloop as the event loop policy
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    logger.debug("lifecycle.uvloop_installed")

    context = AppContext()

    # Initialize Database Engine
    context.db_engine = create_checkcord_engine(context.settings.database_url, echo=False)
    # Initialize Redis Connection Pool / Client
    context.redis_client = redis.asyncio.Redis.from_url(context.settings.redis_url)

    try:
        logger.info("lifecycle.started")
        yield context
    finally:
        logger.info("lifecycle.stopping")
        if context.db_engine is not None:
            await context.db_engine.dispose()
            logger.debug("lifecycle.db_engine_disposed")
        if context.redis_client is not None:
            await context.redis_client.aclose()
            logger.debug("lifecycle.redis_closed")
        logger.info("lifecycle.stopped")
