from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from key_helpers import tenant_keys
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.core.config import get_settings
from checkcord.main import app
from checkcord.storage.models import Skill as DBSkill
from checkcord.storage.postgres import tenant_context

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="module", autouse=True)
def setup_tokens() -> Iterator[dict[str, str]]:
    """Setup settings mapping bearer tokens to tenant UUIDs for ledger tests."""
    tenant_a = str(uuid4())
    tenant_b = str(uuid4())
    token_a = "token_ledger_a"
    token_b = "token_ledger_b"
    tokens = {
        token_a: tenant_a,
        token_b: tenant_b,
    }
    with tenant_keys(tokens):
        yield tokens


@pytest_asyncio.fixture
async def cleanup_db(setup_tokens: dict[str, str]) -> AsyncGenerator[None, None]:
    """Clean up agent_ledgers and skills tables after test run."""
    yield
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        for tenant_id in setup_tokens.values():
            await conn.execute(
                text("DELETE FROM agent_ledgers WHERE tenant_id = :tid"),
                {"tid": UUID(tenant_id)},
            )
            await conn.execute(
                text("DELETE FROM skills WHERE tenant_id = :tid"),
                {"tid": UUID(tenant_id)},
            )
    await engine.dispose()


@pytest.mark.asyncio
async def test_authentication_ledger() -> None:
    """Verify that unauthorized requests to ledger/skills return 401."""
    with TestClient(app) as client:
        # GET ledger without auth
        res = client.get("/api/v1/agents/ledger")
        assert res.status_code == 401

        # POST ledger with invalid bearer token
        res_post = client.post(
            "/api/v1/agents/ledger",
            json={
                "scope_type": "user",
                "scope_id": "test",
                "key": "test_key",
                "value": {},
            },
            headers={"Authorization": "Bearer bad_token"},
        )
        assert res_post.status_code == 401

        # GET skills without auth
        res_skills = client.get("/api/v1/agents/skills")
        assert res_skills.status_code == 401


@pytest.mark.asyncio
async def test_ledger_crud_and_validation(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """Verify ledger upsert, fetch, and payload validation logic."""
    token_a = "token_ledger_a"
    headers_a = {"Authorization": f"Bearer {token_a}"}

    with TestClient(app) as client:
        # 1. Invalid scope type
        res_invalid_scope = client.post(
            "/api/v1/agents/ledger",
            json={
                "scope_type": "invalid_scope",
                "scope_id": "user@tenant.example",
                "key": "my_rules",
                "value": {"rules": ["rule1"]},
            },
            headers=headers_a,
        )
        assert res_invalid_scope.status_code == 400

        # 2. Value too large (exceeds 1MB)
        large_value = {"data": "A" * (1024 * 1024 + 10)}
        res_large_val = client.post(
            "/api/v1/agents/ledger",
            json={
                "scope_type": "user",
                "scope_id": "user@tenant.example",
                "key": "large_key",
                "value": large_value,
            },
            headers=headers_a,
        )
        assert res_large_val.status_code == 413

        # 3. Successful upsert
        upsert_payload = {
            "scope_type": "user",
            "scope_id": "user@tenant.example",
            "key": "coding_rules",
            "value": {"indent": 4, "naming": "snake_case"},
        }
        res_upsert = client.post("/api/v1/agents/ledger", json=upsert_payload, headers=headers_a)
        assert res_upsert.status_code == 200

        # 4. Fetch specific key
        res_fetch = client.get(
            "/api/v1/agents/ledger",
            params={
                "scope_type": "user",
                "scope_id": "user@tenant.example",
                "key": "coding_rules",
            },
            headers=headers_a,
        )
        assert res_fetch.status_code == 200
        data = res_fetch.json()
        assert data["key"] == "coding_rules"
        assert data["value"] == {"indent": 4, "naming": "snake_case"}

        # 5. Missing params when searching key
        res_missing_param = client.get(
            "/api/v1/agents/ledger",
            params={
                "scope_type": "user",
                "key": "coding_rules",
            },
            headers=headers_a,
        )
        assert res_missing_param.status_code == 400

        # 6. Fetch non-existent key
        res_not_found = client.get(
            "/api/v1/agents/ledger",
            params={
                "scope_type": "user",
                "scope_id": "user@tenant.example",
                "key": "non_existent_key",
            },
            headers=headers_a,
        )
        assert res_not_found.status_code == 404

        # 7. Update existing key (upsert)
        update_payload = {
            "scope_type": "user",
            "scope_id": "user@tenant.example",
            "key": "coding_rules",
            "value": {"indent": 2, "naming": "camelCase"},
        }
        res_update = client.post("/api/v1/agents/ledger", json=update_payload, headers=headers_a)
        assert res_update.status_code == 200

        # Fetch again to verify update
        res_fetch_updated = client.get(
            "/api/v1/agents/ledger",
            params={
                "scope_type": "user",
                "scope_id": "user@tenant.example",
                "key": "coding_rules",
            },
            headers=headers_a,
        )
        assert res_fetch_updated.status_code == 200
        assert res_fetch_updated.json()["value"] == {"indent": 2, "naming": "camelCase"}


@pytest.mark.asyncio
async def test_ledger_tenant_isolation(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """Verify that ledger data is isolated between different tenants."""
    token_a = "token_ledger_a"
    token_b = "token_ledger_b"
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    with TestClient(app) as client:
        # Tenant A inserts coding rules
        upsert_payload = {
            "scope_type": "project",
            "scope_id": "payments-service",
            "key": "rules",
            "value": {"rule": "must pass checks"},
        }
        res_a = client.post("/api/v1/agents/ledger", json=upsert_payload, headers=headers_a)
        assert res_a.status_code == 200

        # Tenant B queries key under the same scope -> should return 404 due to RLS/Tenant isolation
        res_b_query = client.get(
            "/api/v1/agents/ledger",
            params={
                "scope_type": "project",
                "scope_id": "payments-service",
                "key": "rules",
            },
            headers=headers_b,
        )
        assert res_b_query.status_code == 404

        # Tenant B lists ledger entries -> should not see Tenant A's entry
        res_b_list = client.get(
            "/api/v1/agents/ledger",
            params={
                "scope_type": "project",
                "scope_id": "payments-service",
            },
            headers=headers_b,
        )
        assert res_b_list.status_code == 200
        assert len(res_b_list.json()) == 0

        # Tenant A lists ledger entries -> should see the entry
        res_a_list = client.get(
            "/api/v1/agents/ledger",
            params={
                "scope_type": "project",
                "scope_id": "payments-service",
            },
            headers=headers_a,
        )
        assert res_a_list.status_code == 200
        assert len(res_a_list.json()) == 1
        assert res_a_list.json()[0]["key"] == "rules"


@pytest.mark.asyncio
async def test_skills_search_and_tenant_isolation(
    setup_tokens: dict[str, str], cleanup_db: AsyncGenerator[None, None]
) -> None:
    """Verify skill search, filtering, and RLS validation."""
    token_a = "token_ledger_a"
    token_b = "token_ledger_b"
    tenant_a = UUID(setup_tokens[token_a])

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    # Pre-insert skills for Tenant A directly in database
    async with tenant_context(engine, tenant_a) as conn:
        # Pre-insert Skill 1
        skill1_stmt = pg_insert(DBSkill.__table__).values(
            id=uuid4(),
            tenant_id=tenant_a,
            name="Deploy payment route",
            description="Steps to mock the DB in tests and define API routes",
            instructions="Deploy steps: ...",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        await conn.execute(skill1_stmt)

        # Pre-insert Skill 2
        skill2_stmt = pg_insert(DBSkill.__table__).values(
            id=uuid4(),
            tenant_id=tenant_a,
            name="Git pull request guide",
            description="Guidelines for opening pulling request in payments repository",
            instructions="Git guide: ...",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        await conn.execute(skill2_stmt)

    await engine.dispose()

    with TestClient(app) as client:
        # 1. Tenant A queries with empty query -> returns both skills
        res_all = client.get("/api/v1/agents/skills", headers=headers_a)
        assert res_all.status_code == 200
        skills_all = res_all.json()
        assert len(skills_all) == 2

        # 2. Tenant A queries for "Deploy" -> returns Skill 1
        res_deploy = client.get(
            "/api/v1/agents/skills", params={"query": "Deploy"}, headers=headers_a
        )
        assert res_deploy.status_code == 200
        skills_deploy = res_deploy.json()
        assert len(skills_deploy) == 1
        assert skills_deploy[0]["name"] == "Deploy payment route"

        # 3. Tenant A queries for "pulling" -> returns Skill 2 (matches description)
        res_pull = client.get(
            "/api/v1/agents/skills", params={"query": "pulling"}, headers=headers_a
        )
        assert res_pull.status_code == 200
        skills_pull = res_pull.json()
        assert len(skills_pull) == 1
        assert skills_pull[0]["name"] == "Git pull request guide"

        # 4. Tenant B queries skills -> returns empty list (tenant isolation)
        res_b = client.get("/api/v1/agents/skills", headers=headers_b)
        assert res_b.status_code == 200
        assert len(res_b.json()) == 0
