"""Tests for the gated admin raw-content view.

Real Postgres and a real HTTP TestClient.
"""

from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from key_helpers import tenant_keys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.core.config import get_settings
from checkcord.main import app
from checkcord.storage.postgres import PostgresEventStore, tenant_context

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="module", autouse=True)
def setup_tokens() -> Iterator[dict[str, str]]:
    tenant_a = str(uuid4())
    token_push = "token_admin_push_a"
    token_admin = "token_admin_privileged_a"
    with tenant_keys({token_push: tenant_a}):
        yield {"tenant_a": tenant_a, "token_push": token_push, "token_admin": token_admin}


@pytest_asyncio.fixture
async def seed_admin_key(setup_tokens: dict[str, str]) -> AsyncGenerator[None, None]:
    """Provision an is_admin=True APIKey row for token_admin."""
    from checkcord.core.crypto import hash_api_key

    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    tenant_a = UUID(setup_tokens["tenant_a"])
    hashed = hash_api_key(setup_tokens["token_admin"])

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text(
                "INSERT INTO api_keys (id, hashed_token, tenant_id, name, active, is_admin) "
                "VALUES (gen_random_uuid(), :hashed, :tenant_id, 'test admin key', true, true)"
            ),
            {"hashed": hashed, "tenant_id": tenant_a},
        )
    await engine.dispose()

    yield

    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text("DELETE FROM api_keys WHERE hashed_token = :hashed"), {"hashed": hashed}
        )
        await conn.execute(
            text("DELETE FROM audit_log_entries WHERE tenant_id = :tid"), {"tid": tenant_a}
        )
        await conn.execute(
            text("DELETE FROM canonical_contexts WHERE tenant_id = :tid"), {"tid": tenant_a}
        )
    await engine.dispose()


async def _seed_context_with_source(tenant_id: UUID) -> UUID:
    from checkcord.models.common import AccessControl, AuthorInfo
    from checkcord.models.enums import AccessVisibility, AclType, SourcePlatform, SourceType
    from checkcord.models.event import CanonicalEvent

    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    ctx_id = uuid4()

    async with tenant_context(engine, tenant_id) as conn:
        store = PostgresEventStore(conn)
        await store.save_canonical_context(
            tenant_id=tenant_id,
            summary="Contact alice@acme.example about this decision",
            confidence_score=8.0,
            id=ctx_id,
        )
        event_id = uuid4()
        event = CanonicalEvent(
            event_id=event_id,
            tenant_id=tenant_id,
            source_platform=SourcePlatform.SLACK,
            source_type=SourceType.MESSAGE,
            source_id="msg_admin_test",
            happened_at=datetime.now(UTC),
            author=AuthorInfo(source_id="user_1", display_name="Alice"),
            content_raw="raw",
            content_clean="raw unredacted content with alice@acme.example in it",
            access_control=AccessControl(
                visibility=AccessVisibility.PUBLIC, acl_type=AclType.CUSTOM, is_public=True
            ),
        )
        await store.save_event(event)
        await store.link_context_to_event(tenant_id, ctx_id, event_id)

    await engine.dispose()
    return ctx_id


async def test_admin_raw_content_requires_auth() -> None:
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/raw-content/context:x?repo_identifier=r")
        assert res.status_code == 401


async def test_admin_raw_content_rejects_ordinary_push_token(
    setup_tokens: dict[str, str],
) -> None:
    """A normal (non-admin) push token must not be able to self-declare admin access."""
    with TestClient(app) as client:
        res = client.get(
            "/api/v1/admin/raw-content/context:x?repo_identifier=r",
            headers={"Authorization": f"Bearer {setup_tokens['token_push']}"},
        )
        assert res.status_code == 403


async def test_admin_raw_content_returns_unredacted_and_writes_audit_entry(
    setup_tokens: dict[str, str], seed_admin_key: None
) -> None:
    tenant_id = UUID(setup_tokens["tenant_a"])
    ctx_id = await _seed_context_with_source(tenant_id)

    with TestClient(app) as client:
        res = client.get(
            f"/api/v1/admin/raw-content/context:{ctx_id}?repo_identifier=irrelevant",
            headers={"Authorization": f"Bearer {setup_tokens['token_admin']}"},
        )
        assert res.status_code == 200
        data = res.json()

    # Unredacted: the raw text, PII included, comes back as-is.
    assert data["summary"] == "Contact alice@acme.example about this decision"
    assert data["sources"][0]["content_clean"] == (
        "raw unredacted content with alice@acme.example in it"
    )

    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with tenant_context(engine, tenant_id) as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT key_name, params, record_ids, result_count FROM audit_log_entries "
                    "WHERE tenant_id = :tid AND action = 'admin.raw_content'"
                ),
                {"tid": tenant_id},
            )
        ).fetchall()
    await engine.dispose()

    assert len(rows) == 1
    key_name, params, record_ids, result_count = rows[0]
    assert key_name == "test admin key"
    assert params == {"node": f"context:{ctx_id}", "repo": "irrelevant"}
    assert str(ctx_id) in record_ids
    assert result_count == 1


async def test_admin_raw_content_not_found(
    setup_tokens: dict[str, str], seed_admin_key: None
) -> None:
    with TestClient(app) as client:
        res = client.get(
            f"/api/v1/admin/raw-content/context:{uuid4()}?repo_identifier=irrelevant",
            headers={"Authorization": f"Bearer {setup_tokens['token_admin']}"},
        )
        assert res.status_code == 404
