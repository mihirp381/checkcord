import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from slack_sdk.oauth.installation_store import Installation
from sqlalchemy import delete, insert, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

import checkcord.cli.commands as cli
from checkcord.adapters.chat.providers.slack.oauth import PostgresInstallationStore
from checkcord.adapters.code.providers.github.connector import connect_github
from checkcord.adapters.meetings.providers.msteams.connector import connect_teams_meetings
from checkcord.adapters.meetings.providers.zoom.connector import connect_zoom
from checkcord.adapters.monitoring.providers.datadog.connector import connect_datadog
from checkcord.adapters.monitoring.providers.sentry.connector import connect_sentry
from checkcord.adapters.tasks.providers.jira.connector import connect_jira
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.cli.secrets import encrypt_stored_secrets
from checkcord.core.config import get_settings
from checkcord.core.crypto import decrypt_secret, decrypt_token, is_encrypted
from checkcord.core.oauth_state import mint_state
from checkcord.main import app
from checkcord.services.keys import create_key
from checkcord.storage.models import (
    APIKey,
    DatadogInstallation,
    GithubInstallation,
    JiraInstallation,
    JiraProjectKey,
    MSTeamsInstallation,
    MsTeamsMeetingInstallation,
    SentryInstallation,
    SlackInstallation,
    ZoomInstallation,
)
from checkcord.storage.postgres import app_bypass_rls_context, create_checkcord_engine

pytestmark = [pytest.mark.integration]

ROLES_SQL = Path(__file__).resolve().parents[2] / "deploy" / "postgres" / "roles.sql"
_TABLES = (
    JiraProjectKey,
    JiraInstallation,
    DatadogInstallation,
    SentryInstallation,
    ZoomInstallation,
    MsTeamsMeetingInstallation,
    GithubInstallation,
    SlackInstallation,
    MSTeamsInstallation,
    APIKey,
)


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_checkcord_engine(get_settings().database_url)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def tenants(engine: AsyncEngine) -> AsyncGenerator[tuple[UUID, UUID], None]:
    pair = (uuid4(), uuid4())
    yield pair
    async with app_bypass_rls_context(engine) as conn:
        for model in _TABLES:
            await conn.execute(delete(model).where(model.tenant_id.in_(pair)))
    redis = Redis.from_url(get_settings().redis_url)
    try:
        await redis.delete("repo.backfill.requested")
    finally:
        await redis.aclose()


@pytest_asyncio.fixture
async def app_engine() -> AsyncGenerator[AsyncEngine, None]:
    admin_url = make_url(get_settings().database_url)
    role, password = f"checkcord_app_test_{uuid4().hex[:8]}", "app-role-test"
    admin = await asyncpg.connect(admin_url.set(drivername="postgresql").render_as_string(False))
    try:
        await admin.execute(ROLES_SQL.read_text())
        await admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' IN ROLE checkcord_app")
        eng = create_checkcord_engine(
            admin_url.set(username=role, password=password).render_as_string(False)
        )
        try:
            yield eng
        finally:
            await eng.dispose()
    finally:
        await admin.execute(f"DROP ROLE IF EXISTS {role}")
        await admin.close()


async def _raw(engine: AsyncEngine, model: Any, *where: Any) -> Any:
    async with app_bypass_rls_context(engine) as conn:
        return (await conn.execute(select(model.__table__).where(*where))).mappings().one()


def _sealed(stored: str, plain: str) -> bool:
    return stored != plain and decrypt_token(stored) == plain


@pytest.mark.asyncio
async def test_connect_writes_encrypted_and_product_readers_decrypt(
    engine: AsyncEngine, tenants: tuple[UUID, UUID]
) -> None:
    tenant = tenants[0]
    host = f"acme-{uuid4().hex[:8]}.atlassian.net"
    jira = await connect_jira(engine, tenant, host, "jira-pat", False, None)
    dd = await connect_datadog(engine, tenant, rotate_secret=False)
    sentry = await connect_sentry(engine, tenant, "sentry-secret")
    account = f"zoom-{uuid4().hex[:8]}"
    await connect_zoom(engine, tenant, account, "zoom-secret")
    ms_tenant = f"ms-{uuid4().hex[:8]}"
    teams = await connect_teams_meetings(engine, tenant, ms_tenant, False)
    repo = f"acme/checkcord-{uuid4().hex[:8]}"
    redis = Redis.from_url(get_settings().redis_url)
    try:
        gh = await connect_github(
            engine, RedisStreamsEventBus(redis), tenant, repo, "ghp_secret", None
        )
    finally:
        await redis.aclose()

    row = await _raw(engine, JiraInstallation, JiraInstallation.jira_host == host)
    assert _sealed(row["webhook_secret"], str(jira.generated_secret))
    assert _sealed(row["personal_access_token"], "jira-pat")
    row = await _raw(engine, DatadogInstallation, DatadogInstallation.id == dd.installation_id)
    assert _sealed(row["webhook_secret"], str(dd.generated_secret))
    row = await _raw(engine, SentryInstallation, SentryInstallation.id == sentry.installation_id)
    assert _sealed(row["webhook_secret"], "sentry-secret")
    row = await _raw(engine, ZoomInstallation, ZoomInstallation.zoom_account_id == account)
    assert _sealed(row["webhook_secret"], "zoom-secret")
    row = await _raw(
        engine, MsTeamsMeetingInstallation, MsTeamsMeetingInstallation.ms_tenant_id == ms_tenant
    )
    assert _sealed(row["webhook_secret"], str(teams.generated_secret))
    row = await _raw(engine, GithubInstallation, GithubInstallation.repo_identifier == repo)
    assert _sealed(row["personal_access_token"], "ghp_secret")
    assert _sealed(row["webhook_secret"], str(gh.generated_secret))

    from checkcord.services.github_tokens import github_token_for

    assert await github_token_for(engine, tenant, repo) == "ghp_secret"

    from checkcord.adapters.tasks.providers.jira import document_poller

    adapter = MagicMock()
    with (
        patch.object(document_poller, "JiraAdapter", adapter),
        patch.object(document_poller, "_issues_after", AsyncMock(return_value=([], False))),
    ):
        await document_poller.poll_jira_documents(engine, AsyncMock())
    pats = {
        c.kwargs["jira_host"]: c.kwargs["personal_access_token"] for c in adapter.call_args_list
    }
    assert pats[host] == "jira-pat"


@pytest.mark.asyncio
async def test_slack_install_encrypted_and_readers_decrypt(
    engine: AsyncEngine, tenants: tuple[UUID, UUID]
) -> None:
    tenant = tenants[0]
    team = f"T{uuid4().hex[:8]}"
    store = PostgresInstallationStore(engine)
    inst = Installation(
        app_id="A1",
        team_id=team,
        bot_token="xoxb-plain",
        bot_id="B1",
        bot_user_id="U0",
        user_token="xoxp-plain",
        user_id="U1",
    )
    await store.save_for_tenant(inst, tenant)

    row = await _raw(engine, SlackInstallation, SlackInstallation.team_id == team)
    assert row["tenant_id"] == tenant
    assert _sealed(row["bot_token"], "xoxb-plain")
    assert "xoxb-plain" not in json.dumps(row["installation_info"])
    assert "xoxp-plain" not in json.dumps(row["installation_info"])

    bot = await store.async_find_bot(enterprise_id=None, team_id=team)
    found = await store.async_find_installation(enterprise_id=None, team_id=team)
    assert bot is not None and bot.bot_token == "xoxb-plain"
    assert found is not None and found.user_token == "xoxp-plain"

    from checkcord.adapters.chat.providers.slack import directory_sync

    client = MagicMock()
    client.return_value.users_list = AsyncMock(return_value={"ok": False})
    client.return_value.conversations_list = AsyncMock(return_value={"ok": False})
    client.return_value.usergroups_list = AsyncMock(return_value={"ok": False})
    with patch.object(directory_sync, "AsyncWebClient", client):
        await directory_sync.sync_slack_directory(engine)
    tokens = {c.kwargs["token"] for c in client.call_args_list}
    assert "xoxb-plain" in tokens

    other = Installation(app_id="A1", team_id=team, bot_token="xoxb-evil", user_id="U9")
    from slack_bolt.error import BoltError

    with pytest.raises(BoltError):
        await store.save_for_tenant(other, tenants[1])
    row = await _raw(engine, SlackInstallation, SlackInstallation.team_id == team)
    assert row["tenant_id"] == tenant and decrypt_secret(row["bot_token"]) == "xoxb-plain"


@pytest.mark.asyncio
async def test_legacy_plaintext_rows_read_then_encrypt_as_app_role(
    engine: AsyncEngine, app_engine: AsyncEngine, tenants: tuple[UUID, UUID]
) -> None:
    tenant = tenants[0]
    dd_id, team = uuid4(), f"T{uuid4().hex[:8]}"
    async with app_bypass_rls_context(engine) as conn:
        await conn.execute(
            insert(DatadogInstallation).values(
                id=dd_id, tenant_id=tenant, webhook_secret="legacy-dd-token"
            )
        )
        await conn.execute(
            insert(SlackInstallation).values(
                team_id=team,
                tenant_id=tenant,
                bot_token="xoxb-legacy",
                installation_info={
                    "app_id": "A1",
                    "team_id": team,
                    "bot_token": "xoxb-legacy",
                    "bot_id": "B1",
                    "bot_user_id": "U0",
                    "installed_at": 1700000000.0,
                },
            )
        )

    url = f"/api/v1/webhooks/datadog/{dd_id}"
    body = json.dumps({"id": "dd-1", "title": "t", "body": "b"}).encode()
    with TestClient(app) as client:
        before = client.post(url, content=body, headers={"X-Datadog-Token": "legacy-dd-token"})
    assert before.status_code == 202, before.text
    bot = await PostgresInstallationStore(engine).async_find_bot(enterprise_id=None, team_id=team)
    assert bot is not None and bot.bot_token == "xoxb-legacy"

    results = {r.table: r for r in await encrypt_stored_secrets(app_engine)}
    assert results["datadog_installations"].encrypted >= 1
    assert results["slack_installations"].encrypted >= 1

    row = await _raw(engine, DatadogInstallation, DatadogInstallation.id == dd_id)
    assert _sealed(row["webhook_secret"], "legacy-dd-token")
    row = await _raw(engine, SlackInstallation, SlackInstallation.team_id == team)
    assert _sealed(row["bot_token"], "xoxb-legacy")
    assert _sealed(row["installation_info"]["bot_token"], "xoxb-legacy")

    with TestClient(app) as client:
        after = client.post(url, content=body, headers={"X-Datadog-Token": "legacy-dd-token"})
        wrong = client.post(url, content=body, headers={"X-Datadog-Token": "nope"})
    assert after.status_code == 202, after.text
    assert wrong.status_code == 401

    again = await encrypt_stored_secrets(app_engine)
    assert all(r.encrypted == 0 for r in again), again
    async with app_bypass_rls_context(engine) as conn:
        rows = (await conn.execute(select(DatadogInstallation.webhook_secret))).scalars()
        assert all(is_encrypted(v) for v in rows)


def test_secrets_encrypt_cli_reports_counts(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["secrets", "encrypt"])
    out = capsys.readouterr().out
    for table in ("slack_installations", "github_installations", "jira_installations"):
        assert table in out
    assert "Encrypted" in out


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_disconnect_requires_admin_of_owning_tenant(
    engine: AsyncEngine, tenants: tuple[UUID, UUID]
) -> None:
    owner, other = tenants
    team, ms_tenant = f"T{uuid4().hex[:8]}", f"ms-{uuid4().hex[:8]}"
    await PostgresInstallationStore(engine).save_for_tenant(
        Installation(app_id="A1", team_id=team, bot_token="xoxb-u", user_id="U1"), owner
    )
    async with app_bypass_rls_context(engine) as conn:
        await conn.execute(
            insert(MSTeamsInstallation).values(
                ms_tenant_id=ms_tenant, tenant_id=owner, auth_payload={}
            )
        )
    _, viewer = await create_key(engine, owner, "viewer", admin=False)
    _, admin = await create_key(engine, owner, "admin", admin=True)
    _, other_admin = await create_key(engine, other, "other admin", admin=True)

    revoke = AsyncMock(return_value={"ok": True})
    with (
        patch("slack_sdk.web.async_client.AsyncWebClient.apps_uninstall", revoke),
        TestClient(app) as client,
    ):
        path = "/api/v1/admin/connections/disconnect"
        for connection in (f"slack:{team}", f"teams:{ms_tenant}"):
            body = {"connection": connection}
            assert client.post(path, json=body).status_code == 401
            assert client.post(path, json=body, headers=_auth(viewer)).status_code == 403
            assert client.post(path, json=body, headers=_auth("chk_not_a_key")).status_code == 401
            assert client.post(path, json=body, headers=_auth(other_admin)).status_code == 404
            assert client.post(path, json=body, headers=_auth(admin)).status_code == 200
            assert client.post(path, json=body, headers=_auth(admin)).status_code == 404
    revoke.assert_awaited_once()

    async with app_bypass_rls_context(engine) as conn:
        assert (
            await conn.scalar(select(SlackInstallation.id).where(SlackInstallation.team_id == team))
            is None
        )
        assert (
            await conn.scalar(
                select(MSTeamsInstallation.id).where(MSTeamsInstallation.ms_tenant_id == ms_tenant)
            )
            is None
        )


def _db(fn: Any) -> Any:
    async def runner() -> Any:
        eng = create_checkcord_engine(get_settings().database_url)
        try:
            return await fn(eng)
        finally:
            await eng.dispose()

    return asyncio.run(runner())


async def _zoom_ids(eng: AsyncEngine, account: str) -> list[UUID]:
    async with app_bypass_rls_context(eng) as conn:
        rows = await conn.execute(
            select(ZoomInstallation.id).where(ZoomInstallation.zoom_account_id == account)
        )
        return list(rows.scalars())


async def _drop_zoom(eng: AsyncEngine, account: str) -> None:
    async with app_bypass_rls_context(eng) as conn:
        await conn.execute(
            delete(ZoomInstallation).where(ZoomInstallation.zoom_account_id == account)
        )


def test_cli_disconnect_uses_tenant_scope(capsys: pytest.CaptureFixture[str]) -> None:
    owner, other = uuid4(), uuid4()
    account = f"zoom-{uuid4().hex[:8]}"
    _db(lambda eng: connect_zoom(eng, owner, account, "zoom-secret"))
    try:
        with pytest.raises(SystemExit):
            cli.main(["disconnect", "zoom", account, "--tenant", str(other)])
        assert len(_db(lambda eng: _zoom_ids(eng, account))) == 1

        cli.main(["disconnect", "zoom", account, "--tenant", str(owner)])
        assert f"Disconnected zoom {account}" in capsys.readouterr().out
        assert _db(lambda eng: _zoom_ids(eng, account)) == []
    finally:
        _db(lambda eng: _drop_zoom(eng, account))


@pytest.mark.asyncio
async def test_teams_callback_binds_link_tenant(
    engine: AsyncEngine, tenants: tuple[UUID, UUID]
) -> None:
    owner, other = tenants
    ms_tenant = f"ms-{uuid4().hex[:8]}"
    url = "/msteams/oauth_redirect?admin_consent=True&tenant=" + ms_tenant + "&state="
    with TestClient(app) as client:
        first = client.get(url + quote(mint_state("teams", owner), safe=""))
        taken = client.get(url + quote(mint_state("teams", other), safe=""))
    assert first.status_code == 200, first.text
    assert taken.status_code == 409
    row = await _raw(engine, MSTeamsInstallation, MSTeamsInstallation.ms_tenant_id == ms_tenant)
    assert row["tenant_id"] == owner
    assert "state" not in row["auth_payload"]


@pytest.mark.asyncio
async def test_worker_token_lookup_as_app_role_is_tenant_scoped(
    engine: AsyncEngine, app_engine: AsyncEngine, tenants: tuple[UUID, UUID]
) -> None:
    from checkcord.services.github_tokens import github_token_for

    tenant, other = tenants
    repo = f"acme/svc-{uuid4().hex[:8]}"
    bus = RedisStreamsEventBus(Redis.from_url(get_settings().redis_url))
    try:
        await connect_github(engine, bus, tenant, repo, "ghp_lookup_secret", None)
    finally:
        await bus.redis.aclose()

    assert await github_token_for(app_engine, tenant, repo) == "ghp_lookup_secret"
    with pytest.raises(LookupError):
        await github_token_for(app_engine, other, repo)
    with pytest.raises(LookupError):
        await github_token_for(None, tenant, repo)
