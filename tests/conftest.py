import asyncio
import os
import time
from collections.abc import AsyncGenerator, Iterator

import fakeredis.aioredis
import pytest
import pytest_asyncio
from langchain_core.embeddings import Embeddings
from redis.asyncio import Redis

from checkcord.api import rate_limit
from checkcord.api.deps import _TOKEN_TENANT_CACHE, _auth_failures
from checkcord.core.config import Settings, get_settings
from checkcord.core.logging import setup_logging
from checkcord.core.rate_limiter import global_rate_limiter

# Set ENVIRONMENT to test so settings default to test and json_format=True
os.environ.setdefault("ENVIRONMENT", "test")
# Tests that exercise the request limits turn them on themselves.
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

# Initialize logging consistently for all tests
setup_logging(log_level="DEBUG", json_format=True)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        marker = item.get_closest_marker("live")
        if marker is None:
            continue
        missing = [key for key in marker.args if not os.environ.get(key)]
        if missing:
            item.add_marker(pytest.mark.skip(reason=f"live test needs {', '.join(missing)}"))


@pytest.fixture(autouse=True)
def reset_global_rate_limiter() -> None:
    """Reset the module-level rate limiter singleton's token buckets before each test.

    `global_rate_limiter` is one shared instance refilled only by wall-clock
    time, so without this its depletion carries across the session and a later
    test can hit a real `asyncio.sleep()` delay, which makes tests order-dependent.
    """
    global_rate_limiter._tpm_tokens = float(global_rate_limiter.max_tpm)
    global_rate_limiter._rpm_tokens = float(global_rate_limiter.max_rpm)
    global_rate_limiter._last_refill = time.monotonic()


@pytest.fixture(autouse=True)
def reset_token_tenant_cache() -> None:
    """Reset api/deps.py's module-level token->tenant TTL cache before each test.

    `_TOKEN_TENANT_CACHE` is a process-global cache keyed by
    `hash_api_key(token)`. Test modules reuse the same literal token strings with
    a fresh tenant_id each, so without this the first module to resolve a token
    would route every other module's requests to its tenant.
    """
    _TOKEN_TENANT_CACHE.clear()
    _auth_failures.clear()
    rate_limit.reset_local()


@pytest.fixture(autouse=True)
def fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def idle_pr_workers(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests run the app with a mocked Redis whose reads return at once."""
    if request.node.get_closest_marker("integration") or "tests/workers/" in str(request.path):
        return

    async def idle(self: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr("checkcord.workers.pr_evaluator.PRWorker.start", idle)
    monkeypatch.setattr("checkcord.workers.pr_status_reporter.PRStatusWorker.start", idle)


@pytest.fixture(autouse=True)
def rls_test_user_for_integration(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("integration"):
        request.getfixturevalue("setup_rls_test_user")


@pytest.fixture(scope="session")
def setup_rls_test_user() -> None:
    """Ensure rls_test_user exists in the database for tests."""
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _setup() -> None:
        settings = get_settings()
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "DO $$\n"
                        "BEGIN\n"
                        "    IF NOT EXISTS (\n"
                        "        SELECT FROM pg_catalog.pg_roles WHERE rolname = 'rls_test_user'\n"
                        "    ) THEN\n"
                        "        CREATE ROLE rls_test_user WITH LOGIN;\n"
                        "    END IF;\n"
                        "END\n"
                        "$$;"
                    )
                )
                await conn.execute(text("GRANT USAGE ON SCHEMA public TO rls_test_user"))
                await conn.execute(
                    text("GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO rls_test_user")
                )
        except Exception as e:
            print(f"Warning: Failed to setup rls_test_user: {e}")
        finally:
            await engine.dispose()

    try:
        asyncio.run(_setup())
    except Exception as e:
        print(f"Warning: Failed to run setup_rls_test_user event loop: {e}")


@pytest.fixture
def test_settings() -> Settings:
    """Provides test settings."""
    return Settings(
        environment="test",
        database_url="postgresql+asyncpg://checkcord:checkcord@localhost:5432/checkcord",
        redis_url="redis://localhost:6379/1",
        log_level="DEBUG",
    )


@pytest_asyncio.fixture
async def async_redis_client(request: pytest.FixtureRequest) -> AsyncGenerator[Redis, None]:
    """Real Redis on localhost for integration tests, fakeredis otherwise."""
    client: Redis
    if request.node.get_closest_marker("integration"):
        client = Redis(host="127.0.0.1", port=6379, db=0)
        await client.ping()
    else:
        client = fakeredis.aioredis.FakeRedis(decode_responses=False)

    yield client
    await client.flushdb()
    await client.aclose()


class DummyEmbeddings(Embeddings):
    """Mock embeddings provider for testing."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 1536 for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return [0.1] * 1536

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 1536 for _ in texts]

    async def aembed_query(self, text: str) -> list[float]:
        return [0.1] * 1536


@pytest.fixture
def dummy_embeddings() -> Embeddings:
    """Provides a dummy Embeddings instance for testing."""
    return DummyEmbeddings()
