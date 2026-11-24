"""Unit and integration tests for the SCIM 2.0 receiver API."""

from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from key_helpers import KeySpec, seeded_keys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.core.config import get_settings
from checkcord.main import app

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="module", autouse=True)
def setup_scim_env() -> str:
    return str(uuid4())


ADMIN_TOKEN = "test_scim_admin_token"
ORDINARY_TOKEN = "test_scim_ordinary_token"


@pytest_asyncio.fixture
async def cleanup_users(setup_scim_env: str) -> AsyncGenerator[None, None]:
    """Seed an admin key, then clean up any users provisioned under the test tenant."""
    tenant = UUID(setup_scim_env)
    keys = [KeySpec(ADMIN_TOKEN, tenant, admin=True), KeySpec(ORDINARY_TOKEN, tenant)]
    async with seeded_keys(keys):
        yield
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        # Superuser bypass to clean up
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text("DELETE FROM enterprise_users WHERE tenant_id = :tid"),
            {"tid": setup_scim_env},
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_scim_token_verification() -> None:
    """Verify that unauthorized requests return flat SCIM 2.0 error responses."""
    with TestClient(app) as client:
        # Missing token
        res = client.post("/scim/v2/Users", json={})
        assert res.status_code == 401
        data = res.json()
        assert "urn:ietf:params:scim:api:messages:2.0:Error" in data["schemas"]
        assert data["status"] == "401"

        # Invalid token
        res2 = client.post("/scim/v2/Users", json={}, headers={"Authorization": "Bearer bad_token"})
        assert res2.status_code == 401
        data2 = res2.json()
        assert data2["status"] == "401"


@pytest.mark.asyncio
async def test_scim_rejects_non_admin_keys(
    setup_scim_env: str, cleanup_users: AsyncGenerator[None, None]
) -> None:
    token = ORDINARY_TOKEN
    payload = {
        "userName": "mallory@example.com",
        "name": {"formatted": "Mallory"},
        "emails": [{"value": "mallory@example.com", "primary": True}],
        "active": True,
    }
    with TestClient(app) as client:
        for res in (
            client.post(
                "/scim/v2/Users", json=payload, headers={"Authorization": f"Bearer {token}"}
            ),
            client.get("/scim/v2/Users", headers={"Authorization": f"Bearer {token}"}),
        ):
            assert res.status_code == 403
            assert res.json()["status"] == "403"
        res_admin = client.get("/scim/v2/Users", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        assert res_admin.status_code == 200
        assert res_admin.json()["totalResults"] == 0


@pytest.mark.asyncio
async def test_scim_user_lifecycle(
    setup_scim_env: str, cleanup_users: AsyncGenerator[None, None]
) -> None:
    """Verify SCIM user creation, get, list, put, and patch endpoints."""
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    with TestClient(app) as client:
        # 1. Provision User
        create_payload = {
            "userName": "bjensen@example.com",
            "name": {
                "formatted": "Barbara Jensen",
                "familyName": "Jensen",
                "givenName": "Barbara",
            },
            "emails": [{"value": "bjensen@example.com", "type": "work", "primary": True}],
            "active": True,
        }
        res_create = client.post("/scim/v2/Users", json=create_payload, headers=headers)
        assert res_create.status_code == 201
        data = res_create.json()
        assert data["userName"] == "bjensen@example.com"
        assert data["active"] is True
        user_id = data["id"]

        # Duplicate Provision -> 409 Conflict
        res_dup = client.post("/scim/v2/Users", json=create_payload, headers=headers)
        assert res_dup.status_code == 409
        assert res_dup.json()["status"] == "409"

        # 2. Get User
        res_get = client.get(f"/scim/v2/Users/{user_id}", headers=headers)
        assert res_get.status_code == 200
        assert res_get.json()["id"] == user_id

        # Get missing user -> 404 Not Found
        res_get_missing = client.get(f"/scim/v2/Users/{uuid4()}", headers=headers)
        assert res_get_missing.status_code == 404
        assert res_get_missing.json()["status"] == "404"

        # 3. List Users
        res_list = client.get("/scim/v2/Users", headers=headers)
        assert res_list.status_code == 200
        assert res_list.json()["totalResults"] == 1

        # List with filter matching email
        res_list_filter = client.get(
            '/scim/v2/Users?filter=userName eq "bjensen@example.com"', headers=headers
        )
        assert res_list_filter.status_code == 200
        assert res_list_filter.json()["totalResults"] == 1

        # List with filter not matching
        res_list_filter_none = client.get(
            '/scim/v2/Users?filter=userName eq "nonexistent@example.com"', headers=headers
        )
        assert res_list_filter_none.status_code == 200
        assert res_list_filter_none.json()["totalResults"] == 0

        # 4. Update User (PUT)
        update_payload = {
            "userName": "bjensen@example.com",
            "name": {"formatted": "Barbara J. Jensen"},
            "emails": [{"value": "bjensen@example.com", "primary": True}],
            "active": False,
        }
        res_put = client.put(f"/scim/v2/Users/{user_id}", json=update_payload, headers=headers)
        assert res_put.status_code == 200
        assert res_put.json()["name"]["formatted"] == "Barbara J. Jensen"
        assert res_put.json()["active"] is False

        # 5. Patch User (PATCH)
        # Patch replacing active = True (using path="active")
        patch_payload_1 = {"Operations": [{"op": "replace", "path": "active", "value": True}]}
        res_patch_1 = client.patch(
            f"/scim/v2/Users/{user_id}", json=patch_payload_1, headers=headers
        )
        assert res_patch_1.status_code == 200
        assert res_patch_1.json()["active"] is True

        # Patch replacing active = False (using value dict)
        patch_payload_2 = {"Operations": [{"op": "replace", "value": {"active": False}}]}
        res_patch_2 = client.patch(
            f"/scim/v2/Users/{user_id}", json=patch_payload_2, headers=headers
        )
        assert res_patch_2.status_code == 200
        assert res_patch_2.json()["active"] is False

        # Patch replacing displayName / name / emails
        patch_payload_3 = {
            "Operations": [
                {"op": "replace", "path": "displayName", "value": "Jane Jensen"},
                {"op": "replace", "path": "name.givenName", "value": "Jane"},
                {
                    "op": "replace",
                    "path": "emails",
                    "value": [{"value": "jane@example.com", "primary": True}],
                },
            ]
        }
        res_patch_3 = client.patch(
            f"/scim/v2/Users/{user_id}", json=patch_payload_3, headers=headers
        )
        assert res_patch_3.status_code == 200
        assert res_patch_3.json()["name"]["givenName"] == "Jane"
        assert res_patch_3.json()["emails"][0]["value"] == "jane@example.com"

        # Verify changes persisted on GET
        res_get_final = client.get(f"/scim/v2/Users/{user_id}", headers=headers)
        assert res_get_final.status_code == 200
        assert res_get_final.json()["name"]["givenName"] == "Jane"
        assert res_get_final.json()["emails"][0]["value"] == "jane@example.com"
