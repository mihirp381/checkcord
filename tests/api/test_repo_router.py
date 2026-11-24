from collections.abc import AsyncGenerator, Iterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from key_helpers import KeySpec, seeded_keys, tenant_keys
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.core.config import get_settings
from checkcord.core.crypto import decrypt_secret
from checkcord.main import app
from checkcord.models.repo_backfill import RepoBackfillRequestPayload

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

REPO_NAME = "test_org/connect_test_repo"


@pytest.fixture(scope="module", autouse=True)
def setup_tokens() -> Iterator[dict[str, str]]:
    tenant_a = str(uuid4())
    tenant_b = str(uuid4())
    token_a = "token_repo_connect_a"
    token_b = "token_repo_connect_b"
    tokens = {token_a: tenant_a, token_b: tenant_b}
    with tenant_keys(tokens):
        yield tokens


def _admin(token: str) -> str:
    return f"admin_{token}"


@pytest_asyncio.fixture
async def admin_keys(setup_tokens: dict[str, str]) -> AsyncGenerator[None, None]:
    specs = [KeySpec(_admin(t), UUID(tid), admin=True) for t, tid in setup_tokens.items()]
    async with seeded_keys(specs):
        yield


@pytest_asyncio.fixture
async def cleanup(setup_tokens: dict[str, str], admin_keys: None) -> AsyncGenerator[None, None]:
    settings = get_settings()
    redis_client = Redis.from_url(settings.redis_url)
    try:
        await redis_client.delete("repo.backfill.requested")
    except Exception:
        pass
    await redis_client.aclose()

    yield

    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text("DELETE FROM github_installations WHERE repo_identifier = :repo"),
            {"repo": REPO_NAME},
        )
    await engine.dispose()

    redis_client = Redis.from_url(settings.redis_url)
    try:
        await redis_client.delete("repo.backfill.requested")
    except Exception:
        pass
    await redis_client.aclose()


async def _drain_backfill_requests(redis_url: str) -> list[RepoBackfillRequestPayload]:
    client = Redis.from_url(redis_url)
    try:
        raw = await client.xread(streams={"repo.backfill.requested": "0-0"}, count=100)
        payloads = []
        if raw:
            for _, messages in raw:
                for _msg_id, fields in messages:
                    data = fields.get(b"data") or fields.get("data")
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")
                    payloads.append(RepoBackfillRequestPayload.model_validate_json(data))
        return payloads
    finally:
        await client.aclose()


async def test_connect_repo_requires_auth() -> None:
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/admin/repos/connect",
            json={
                "repo_identifier": REPO_NAME,
                "personal_access_token": "pat",
                "webhook_secret": "secret",
            },
        )
    assert res.status_code == 401


async def test_connect_repo_rejects_ordinary_key(
    setup_tokens: dict[str, str], cleanup: AsyncGenerator[None, None]
) -> None:
    settings = get_settings()
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/admin/repos/connect",
            json={
                "repo_identifier": REPO_NAME,
                "personal_access_token": "pat",
                "webhook_secret": "secret",
            },
            headers={"Authorization": "Bearer token_repo_connect_a"},
        )
    assert res.status_code == 403
    assert res.json()["detail"] == "Forbidden: admin rights required"

    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        count = await conn.scalar(
            text("SELECT count(*) FROM github_installations WHERE repo_identifier = :repo"),
            {"repo": REPO_NAME},
        )
    await engine.dispose()
    assert count == 0
    assert await _drain_backfill_requests(settings.redis_url) == []


async def test_connect_repo_creates_installation_and_triggers_backfill(
    setup_tokens: dict[str, str], cleanup: AsyncGenerator[None, None]
) -> None:
    settings = get_settings()
    token_a = "token_repo_connect_a"
    tenant_a = UUID(setup_tokens[token_a])
    headers = {"Authorization": f"Bearer {_admin(token_a)}"}

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/admin/repos/connect",
            json={
                "repo_identifier": REPO_NAME,
                "personal_access_token": "pat_123",
                "webhook_secret": "secret_123",
            },
            headers=headers,
        )

    assert res.status_code == 202, res.text
    assert res.json() == {"status": "connected", "repo_identifier": REPO_NAME}

    # GithubInstallation row exists, scoped to the caller's own tenant.
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT tenant_id, personal_access_token, webhook_secret "
                        "FROM github_installations WHERE repo_identifier = :repo"
                    ),
                    {"repo": REPO_NAME},
                )
            )
            .mappings()
            .fetchone()
        )
    await engine.dispose()

    assert row is not None
    assert row["tenant_id"] == tenant_a
    assert row["personal_access_token"] != "pat_123"
    assert decrypt_secret(row["personal_access_token"]) == "pat_123"
    assert row["webhook_secret"] != "secret_123"
    assert decrypt_secret(row["webhook_secret"]) == "secret_123"

    # Exactly one backfill request was published for the caller's tenant/repo.
    payloads = await _drain_backfill_requests(settings.redis_url)
    assert len(payloads) == 1
    assert payloads[0].tenant_id == tenant_a
    assert payloads[0].repo_identifier == REPO_NAME


async def test_connect_repo_cross_tenant_conflict_rejected(
    setup_tokens: dict[str, str], cleanup: AsyncGenerator[None, None]
) -> None:
    """Tenant B must not be able to hijack a repo already connected to tenant A."""
    token_a = "token_repo_connect_a"
    token_b = "token_repo_connect_b"
    headers_a = {"Authorization": f"Bearer {_admin(token_a)}"}
    headers_b = {"Authorization": f"Bearer {_admin(token_b)}"}

    with TestClient(app) as client:
        res_a = client.post(
            "/api/v1/admin/repos/connect",
            json={
                "repo_identifier": REPO_NAME,
                "personal_access_token": "pat_a",
                "webhook_secret": "secret_a",
            },
            headers=headers_a,
        )
        assert res_a.status_code == 202

        res_b = client.post(
            "/api/v1/admin/repos/connect",
            json={
                "repo_identifier": REPO_NAME,
                "personal_access_token": "pat_b",
                "webhook_secret": "secret_b",
            },
            headers=headers_b,
        )

    assert res_b.status_code == 409


async def test_connect_repo_same_tenant_reconnect_updates_token(
    setup_tokens: dict[str, str], cleanup: AsyncGenerator[None, None]
) -> None:
    """Re-connecting the same repo under the same tenant refreshes the PAT/secret
    (e.g. after a token rotation) rather than erroring."""
    settings = get_settings()
    token_a = "token_repo_connect_a"
    tenant_a = UUID(setup_tokens[token_a])
    headers = {"Authorization": f"Bearer {_admin(token_a)}"}

    with TestClient(app) as client:
        res1 = client.post(
            "/api/v1/admin/repos/connect",
            json={
                "repo_identifier": REPO_NAME,
                "personal_access_token": "pat_old",
                "webhook_secret": "secret_old",
            },
            headers=headers,
        )
        assert res1.status_code == 202

        res2 = client.post(
            "/api/v1/admin/repos/connect",
            json={
                "repo_identifier": REPO_NAME,
                "personal_access_token": "pat_new",
                "webhook_secret": "secret_new",
            },
            headers=headers,
        )
        assert res2.status_code == 202

    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT tenant_id, personal_access_token FROM github_installations "
                        "WHERE repo_identifier = :repo"
                    ),
                    {"repo": REPO_NAME},
                )
            )
            .mappings()
            .fetchone()
        )
    await engine.dispose()

    assert row is not None
    assert row["tenant_id"] == tenant_a
    assert decrypt_secret(row["personal_access_token"]) == "pat_new"

    # Two connects -> two backfill requests published (both legitimate triggers).
    payloads = await _drain_backfill_requests(settings.redis_url)
    assert len(payloads) == 2
