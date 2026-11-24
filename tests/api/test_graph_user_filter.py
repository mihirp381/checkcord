from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app_role_helpers import app_database_url, create_app_role, drop_app_role
from fastapi.testclient import TestClient
from key_helpers import KeySpec, delete_contexts, seed_context, seeded_keys

from checkcord.main import app

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

REPO = "acme/viewer-filter"


@dataclass(frozen=True)
class Contexts:
    tenant_a: UUID
    tenant_b: UUID
    public: UUID
    alice_only: UUID
    other_tenant: UUID


@pytest_asyncio.fixture
async def contexts() -> AsyncGenerator[Contexts, None]:
    tenant_a, tenant_b = uuid4(), uuid4()
    specs = [
        KeySpec("graph_filter_alice", tenant_a, user_email="alice@acme.example"),
        KeySpec("graph_filter_bob", tenant_a, user_email="bob@acme.example"),
        KeySpec("graph_filter_unbound", tenant_a),
        KeySpec("graph_filter_admin", tenant_a, admin=True),
        KeySpec("graph_filter_b", tenant_b),
    ]
    async with seeded_keys(specs) as users:
        c = Contexts(
            tenant_a=tenant_a,
            tenant_b=tenant_b,
            public=await seed_context(tenant_a, "VIEWER-PUBLIC", public=True),
            alice_only=await seed_context(
                tenant_a, "VIEWER-ALICE-ONLY", public=False, allowed=[users["alice@acme.example"]]
            ),
            other_tenant=await seed_context(tenant_b, "VIEWER-TENANT-B", public=True),
        )
        role, url = await create_app_role()
        try:
            with app_database_url(url):
                yield c
        finally:
            await drop_app_role(role)
            await delete_contexts(tenant_a, tenant_b)


def _node(client: TestClient, token: str, ctx_id: UUID) -> int:
    res = client.get(
        f"/api/v1/graph/node/context:{ctx_id}?repo_identifier={REPO}",
        headers={"Authorization": f"Bearer {token}"},
    )
    return res.status_code


def _decisions(client: TestClient, token: str) -> str:
    res = client.get(
        f"/api/v1/graph/decisions/active?repo_identifier={REPO}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text
    return str(res.json()["markdown_content"])


@pytest.mark.parametrize(
    ("token", "sees_alice_only"),
    [
        ("graph_filter_alice", True),
        ("graph_filter_bob", False),
        ("graph_filter_unbound", False),
        ("graph_filter_admin", False),
    ],
)
async def test_graph_reads_follow_the_keys_identity(
    contexts: Contexts, token: str, sees_alice_only: bool
) -> None:
    with TestClient(app) as client:
        assert _node(client, token, contexts.public) == 200
        assert _node(client, token, contexts.alice_only) == (200 if sees_alice_only else 404)
        assert _node(client, token, contexts.other_tenant) == 404
        markdown = _decisions(client, token)
    assert "VIEWER-PUBLIC" in markdown
    assert ("VIEWER-ALICE-ONLY" in markdown) is sees_alice_only
    assert "VIEWER-TENANT-B" not in markdown


async def test_admin_raw_content_stays_unfiltered(contexts: Contexts) -> None:
    with TestClient(app) as client:
        res = client.get(
            f"/api/v1/admin/raw-content/context:{contexts.alice_only}?repo_identifier={REPO}",
            headers={"Authorization": "Bearer graph_filter_admin"},
        )
    assert res.status_code == 200, res.text
    assert "VIEWER-ALICE-ONLY" in res.text
