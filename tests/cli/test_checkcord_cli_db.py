import asyncio
import hashlib
import hmac
import json
import re
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

import checkcord.cli.commands as cli
from checkcord.adapters.code.providers.github.connector import connect_github
from checkcord.adapters.meetings.providers.msteams.connector import connect_teams_meetings
from checkcord.adapters.meetings.providers.zoom.connector import connect_zoom
from checkcord.adapters.meetings.providers.zoom.connector import webhook_url as zoom_webhook_url
from checkcord.adapters.monitoring.providers.datadog.connector import connect_datadog
from checkcord.adapters.monitoring.providers.sentry.connector import connect_sentry
from checkcord.adapters.tasks.providers.jira.connector import connect_jira
from checkcord.api.deps import (
    _TOKEN_TENANT_CACHE,
    Principal,
    require_admin_principal,
    resolve_principal,
    verify_hybrid_bearer_token,
)
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.connectors.cli import OwnedByOtherTenantError
from checkcord.core.config import get_settings
from checkcord.core.crypto import decrypt_token
from checkcord.main import app
from checkcord.storage.models import (
    APIKey,
    AuditLogEntry,
    CanonicalContext,
    ChatUser,
    ConnectionAudience,
    ConnectionReview,
    ConnectionScope,
    ConnectionScopeSkip,
    DatadogInstallation,
    EnterpriseUser,
    GithubInstallation,
    JiraInstallation,
    JiraProjectKey,
    MsTeamsMeetingInstallation,
    RecordReplacementSuggestion,
    SentryInstallation,
    ZoomInstallation,
)
from checkcord.storage.postgres import app_bypass_rls_context, create_checkcord_engine

pytestmark = [pytest.mark.integration]

_TOUCHED_STREAMS = ("events.raw.tasks", "events.raw.monitoring", "repo.backfill.requested")
_TENANT_TABLES = (
    ChatUser,
    ConnectionAudience,
    JiraProjectKey,
    JiraInstallation,
    DatadogInstallation,
    SentryInstallation,
    ZoomInstallation,
    MsTeamsMeetingInstallation,
    GithubInstallation,
    APIKey,
    EnterpriseUser,
    AuditLogEntry,
    ConnectionReview,
    ConnectionScope,
    ConnectionScopeSkip,
    RecordReplacementSuggestion,
    CanonicalContext,
)


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_checkcord_engine(get_settings().database_url)
    yield eng
    await eng.dispose()


def _db[T](fn: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def runner() -> T:
        eng = create_checkcord_engine(get_settings().database_url)
        try:
            return await fn(eng)
        finally:
            await eng.dispose()

    return asyncio.run(runner())


async def _cleanup(eng: AsyncEngine, tenant_id: UUID) -> None:
    async with app_bypass_rls_context(eng) as conn:
        await conn.execute(text("SET LOCAL app.include_retired = 'true'"))
        for model in _TENANT_TABLES:
            await conn.execute(delete(model).where(model.tenant_id == tenant_id))
    redis = Redis.from_url(get_settings().redis_url)
    try:
        await redis.delete(*_TOUCHED_STREAMS)
    finally:
        await redis.aclose()


@pytest.fixture
def tenant() -> Iterator[UUID]:
    tenant_id = uuid4()
    yield tenant_id
    _db(lambda eng: _cleanup(eng, tenant_id))


@asynccontextmanager
async def _probe_lifespan(api: FastAPI) -> AsyncGenerator[None, None]:
    api.state.db_engine = create_checkcord_engine(get_settings().database_url)
    yield
    await api.state.db_engine.dispose()


@pytest.fixture
def probe() -> Iterator[TestClient]:
    api = FastAPI(lifespan=_probe_lifespan)

    @api.get("/whoami")
    async def whoami(tenant_id: UUID = Depends(verify_hybrid_bearer_token)) -> dict[str, str]:
        return {"tenant": str(tenant_id)}

    @api.get("/admin")
    async def admin(principal: Principal = Depends(require_admin_principal)) -> dict[str, str]:
        return {"tenant": str(principal.tenant_id)}

    with TestClient(api) as client:
        yield client


def _clear_token_caches() -> None:
    _TOKEN_TENANT_CACHE.clear()


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    cli.main(argv)
    return capsys.readouterr().out


def _token(output: str) -> str:
    match = re.search(r"^\s+(chk_\S+)$", output, re.M)
    assert match, output
    return match.group(1)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_created_key_authenticates_and_revoked_key_is_rejected(
    tenant: UUID, probe: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _run(["keys", "create", "--name", "ci viewer", "--tenant", str(tenant)], capsys)
    token = _token(out)

    response = probe.get("/whoami", headers=_auth(token))
    assert response.status_code == 200
    assert response.json() == {"tenant": str(tenant)}
    assert probe.get("/admin", headers=_auth(token)).status_code == 403

    listed = _run(["keys", "list", "--tenant", str(tenant)], capsys)
    assert "ci viewer" in listed and "active" in listed and token not in listed
    key_id = re.search(r"^([0-9a-f-]{36})\s", listed, re.M)
    assert key_id

    revoked = _run(["keys", "revoke", key_id.group(1), "--tenant", str(tenant)], capsys)
    assert "300 seconds" in revoked
    _clear_token_caches()
    assert probe.get("/whoami", headers=_auth(token)).status_code == 401
    assert "revoked" in _run(["keys", "list", "--tenant", str(tenant)], capsys)


def _users(tenant: UUID) -> list[tuple[str, bool]]:
    async def fetch(eng: AsyncEngine) -> list[tuple[str, bool]]:
        async with app_bypass_rls_context(eng) as conn:
            rows = await conn.execute(
                select(EnterpriseUser.primary_email, EnterpriseUser.active).where(
                    EnterpriseUser.tenant_id == tenant
                )
            )
            return [(r[0], r[1]) for r in rows]

    return _db(fetch)


def test_key_bound_to_unknown_user_is_refused(
    tenant: UUID, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "keys",
                "create",
                "--name",
                "k",
                "--user",
                "nobody@acme.example",
                "--tenant",
                str(tenant),
            ]
        )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "nobody@acme.example" in err and "--create-user" in err
    assert _users(tenant) == []
    assert "chk_" not in _run(["keys", "list", "--tenant", str(tenant)], capsys)


def test_key_bound_to_user_resolves_to_that_user(
    tenant: UUID, probe: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["keys", "create", "--name", "alice laptop", "--user", "alice@acme.example"]
    out = _run([*argv, "--create-user", "--tenant", str(tenant)], capsys)
    token = _token(out)
    assert "alice@acme.example" in out
    assert _users(tenant) == [("alice@acme.example", True)]

    # A second key for the same, now existing, user needs no --create-user.
    second = _token(_run([*argv, "--tenant", str(tenant)], capsys))
    assert _users(tenant) == [("alice@acme.example", True)]

    listed = _run(["keys", "list", "--tenant", str(tenant)], capsys)
    assert listed.count("alice@acme.example") == 2

    async def principal(eng: AsyncEngine, tok: str) -> Any:
        return await resolve_principal(tok, eng)

    for tok in (token, second):
        p = _db(lambda eng, tok=tok: principal(eng, tok))
        assert p is not None and p.tenant_id == tenant
        assert p.user_email == "alice@acme.example" and p.user_id is not None
        assert p.is_admin is False
        assert probe.get("/whoami", headers=_auth(tok)).status_code == 200


def test_key_of_deactivated_user_is_rejected(
    tenant: UUID, probe: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["keys", "create", "--name", "k", "--user", "carol@acme.example", "--create-user"]
    token = _token(_run([*argv, "--tenant", str(tenant)], capsys))
    assert probe.get("/whoami", headers=_auth(token)).status_code == 200

    async def deactivate(eng: AsyncEngine) -> None:
        async with app_bypass_rls_context(eng) as conn:
            await conn.execute(
                EnterpriseUser.__table__.update()
                .where(EnterpriseUser.tenant_id == tenant)
                .values(active=False)
            )

    _db(deactivate)
    _clear_token_caches()
    assert probe.get("/whoami", headers=_auth(token)).status_code == 401

    with pytest.raises(SystemExit):
        cli.main(
            [
                "keys",
                "create",
                "--name",
                "k2",
                "--user",
                "carol@acme.example",
                "--tenant",
                str(tenant),
            ]
        )
    with pytest.raises(SystemExit):
        cli.main([*argv, "--tenant", str(tenant)])
    assert _users(tenant) == [("carol@acme.example", False)]


def test_revoke_is_tenant_scoped(tenant: UUID, capsys: pytest.CaptureFixture[str]) -> None:
    out = _run(["keys", "create", "--name", "k", "--tenant", str(tenant)], capsys)
    key_id = re.search(r"API key ([0-9a-f-]{36})", out)
    assert key_id
    with pytest.raises(SystemExit) as exc:
        cli.main(["keys", "revoke", key_id.group(1), "--tenant", str(uuid4())])
    assert exc.value.code == 1

    async def active(eng: AsyncEngine) -> Any:
        async with app_bypass_rls_context(eng) as conn:
            return await conn.scalar(
                select(APIKey.active).where(APIKey.id == UUID(key_id.group(1)))
            )

    assert _db(active) is True


def test_init_is_safe_to_rerun(
    tenant: UUID, probe: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    async def admin_count(eng: AsyncEngine) -> int:
        async with app_bypass_rls_context(eng) as conn:
            count = await conn.scalar(
                select(func.count())
                .select_from(APIKey)
                .where(APIKey.tenant_id == tenant, APIKey.is_admin, APIKey.active)
            )
            return int(count or 0)

    token = _token(_run(["init", "--tenant", str(tenant)], capsys))
    assert probe.get("/admin", headers=_auth(token)).json() == {"tenant": str(tenant)}

    again = _run(["init", "--tenant", str(tenant)], capsys)
    assert "already has an active admin key" in again
    assert "chk_" not in again
    assert _db(admin_count) == 1

    forced = _token(_run(["init", "--force", "--tenant", str(tenant)], capsys))
    assert forced != token
    assert _db(admin_count) == 2


def _hmac_hex(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_jira_round_trip(tenant: UUID, engine: AsyncEngine) -> None:
    host = f"acme-{uuid4().hex[:8]}.atlassian.net"
    result = await connect_jira(engine, tenant, f"https://{host}/", "jira-pat", False, ["eng"])
    assert result.created and result.generated_secret
    secret = result.generated_secret

    again = await connect_jira(engine, tenant, host, "jira-pat-2", False, ["ENG"])
    assert (again.installation_id, again.created, again.generated_secret) == (
        result.installation_id,
        False,
        None,
    )
    async with app_bypass_rls_context(engine) as conn:
        row = (
            await conn.execute(
                select(
                    JiraInstallation.webhook_secret, JiraInstallation.personal_access_token
                ).where(JiraInstallation.jira_host == host)
            )
        ).one()
        keys = (
            await conn.execute(
                select(JiraProjectKey.project_key).where(JiraProjectKey.tenant_id == tenant)
            )
        ).scalars()
        assert list(keys) == ["ENG"]
    assert row.webhook_secret != secret and decrypt_token(row.webhook_secret) == secret
    assert row.personal_access_token != "jira-pat-2"
    assert decrypt_token(row.personal_access_token) == "jira-pat-2"

    body = json.dumps(
        {
            "webhookEvent": "jira:issue_created",
            "timestamp": 1719225840000,
            "user": {"accountId": "u1", "displayName": "Dev"},
            "issue": {
                "id": "10002",
                "key": "ENG-42",
                "self": f"https://{host}/rest/api/2/issue/10002",
                "fields": {"summary": "Fix auth loop", "status": {"name": "To Do"}},
            },
        }
    ).encode()
    with TestClient(app) as client:
        good = client.post(
            "/api/v1/webhooks/jira",
            content=body,
            headers={"X-Hub-Signature-256": f"sha256={_hmac_hex(secret, body)}"},
        )
        bad = client.post(
            "/api/v1/webhooks/jira",
            content=body,
            headers={"X-Hub-Signature-256": f"sha256={_hmac_hex('wrong', body)}"},
        )
    assert good.status_code == 202, good.text
    assert bad.status_code == 401


@pytest.mark.asyncio
async def test_datadog_round_trip(tenant: UUID, engine: AsyncEngine) -> None:
    result = await connect_datadog(engine, tenant, rotate_secret=False)
    assert result.created and result.generated_secret
    assert (await connect_datadog(engine, tenant, rotate_secret=False)).generated_secret is None
    rotated = await connect_datadog(engine, tenant, rotate_secret=True)
    assert rotated.installation_id == result.installation_id
    assert rotated.generated_secret and rotated.generated_secret != result.generated_secret

    url = f"/api/v1/webhooks/datadog/{result.installation_id}"
    body = json.dumps({"id": "dd-1", "title": "high error rate", "body": "details"}).encode()
    with TestClient(app) as client:
        good = client.post(url, content=body, headers={"X-Datadog-Token": rotated.generated_secret})
        stale = client.post(
            url, content=body, headers={"X-Datadog-Token": str(result.generated_secret)}
        )
    assert good.status_code == 202, good.text
    assert stale.status_code == 401


@pytest.mark.asyncio
async def test_sentry_round_trip(tenant: UUID, engine: AsyncEngine) -> None:
    result = await connect_sentry(engine, tenant, "sentry-client-secret")
    assert result.created and result.generated_secret is None

    url = f"/api/v1/webhooks/sentry/{result.installation_id}"
    body = json.dumps(
        {"id": "s-1", "level": "error", "message": "ValueError: boom", "project": "api"}
    ).encode()
    with TestClient(app) as client:
        good = client.post(
            url,
            content=body,
            headers={"Sentry-Hook-Signature": _hmac_hex("sentry-client-secret", body)},
        )
        bad = client.post(
            url, content=body, headers={"Sentry-Hook-Signature": _hmac_hex("other", body)}
        )
    assert good.status_code == 202, good.text
    assert bad.status_code == 401


@pytest.mark.asyncio
async def test_zoom_round_trip(tenant: UUID, engine: AsyncEngine) -> None:
    account = f"zoom-{uuid4().hex[:8]}"
    result = await connect_zoom(engine, tenant, account, "zoom-secret-token")
    assert result.created
    with pytest.raises(OwnedByOtherTenantError):
        await connect_zoom(engine, uuid4(), account, "x")

    body = json.dumps(
        {"event": "endpoint.url_validation", "payload": {"plainToken": "plain"}}
    ).encode()
    ts = str(int(time.time()))

    def sig(secret: str) -> str:
        return "v0=" + _hmac_hex(secret, f"v0:{ts}:{body.decode()}".encode())

    url = zoom_webhook_url("http://testserver", account)
    with TestClient(app) as client:
        good = client.post(
            url,
            content=body,
            headers={"x-zm-request-timestamp": ts, "x-zm-signature": sig("zoom-secret-token")},
        )
        bad = client.post(
            url,
            content=body,
            headers={"x-zm-request-timestamp": ts, "x-zm-signature": sig("wrong")},
        )
    assert good.status_code == 200, good.text
    assert good.json()["encryptedToken"] == _hmac_hex("zoom-secret-token", b"plain")
    assert bad.status_code == 401


@pytest.mark.asyncio
async def test_teams_meetings_round_trip(tenant: UUID, engine: AsyncEngine) -> None:
    ms_tenant = f"ms-{uuid4().hex[:8]}"
    result = await connect_teams_meetings(engine, tenant, ms_tenant, rotate_secret=False)
    assert result.created and result.generated_secret
    secret = result.generated_secret

    def notification(client_state: str) -> dict[str, Any]:
        resource = "users/u1/onlineMeetings/m1/transcripts/t1"
        note = {"tenantId": ms_tenant, "resource": resource, "clientState": client_state}
        return {"value": [note]}

    token = AsyncMock(side_effect=RuntimeError("no Graph access in tests"))
    target = "checkcord.adapters.meetings.providers.msteams.webhook.get_msteams_access_token"
    with patch(target, token), TestClient(app) as client:
        bad = client.post("/api/v1/webhooks/msteams/meetings", json=notification("wrong"))
        assert bad.status_code == 202
        token.assert_not_awaited()
        good = client.post("/api/v1/webhooks/msteams/meetings", json=notification(secret))
        assert good.status_code == 202
        token.assert_awaited_once()
    assert token.await_args is not None
    assert token.await_args.kwargs["ms_tenant_id"] == ms_tenant


@pytest.mark.asyncio
async def test_github_round_trip(tenant: UUID, engine: AsyncEngine) -> None:
    repo = f"acme/checkcord-{uuid4().hex[:8]}"
    redis = Redis.from_url(get_settings().redis_url)
    try:
        result = await connect_github(
            engine, RedisStreamsEventBus(redis), tenant, repo, "ghp_test", None
        )
        backfills = await redis.xrange("repo.backfill.requested")
    finally:
        await redis.aclose()
    assert result.generated_secret
    assert any(repo.encode() in b"".join(fields.values()) for _, fields in backfills)
    async with app_bypass_rls_context(engine) as conn:
        stored = await conn.scalar(
            select(GithubInstallation.tenant_id).where(GithubInstallation.repo_identifier == repo)
        )
    assert stored == tenant

    body = json.dumps({"zen": "hi", "repository": {"full_name": repo}}).encode()
    with TestClient(app) as client:
        good = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "ping",
                "X-Hub-Signature-256": f"sha256={_hmac_hex(result.generated_secret, body)}",
            },
        )
        bad = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "ping",
                "X-Hub-Signature-256": f"sha256={_hmac_hex('wrong', body)}",
            },
        )
    assert good.status_code == 200, good.text
    assert good.json() == {"status": "ok"}
    assert bad.status_code == 401


def test_connections_lists_without_secrets(
    tenant: UUID, capsys: pytest.CaptureFixture[str]
) -> None:
    account = f"zoom-{uuid4().hex[:8]}"
    _db(lambda eng: connect_zoom(eng, tenant, account, "zoom-secret-token"))
    dd = _db(lambda eng: connect_datadog(eng, tenant, rotate_secret=False))
    out = _run(["connections", "--tenant", str(tenant)], capsys)
    assert account in out and str(dd.installation_id) in out
    assert "zoom-secret-token" not in out and str(dd.generated_secret) not in out


def test_connect_with_review_and_toggle(
    tenant: UUID, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    account = f"zoom-{uuid4().hex[:8]}"
    monkeypatch.setenv("CHECKCORD_ZOOM_SECRET_TOKEN", "zoom-secret-token")
    out = _run(
        [
            "connect",
            "zoom",
            "--account-id",
            account,
            "--review",
            "--public-url",
            "https://checkcord.example.com",
            "--tenant",
            str(tenant),
        ],
        capsys,
    )
    assert f"In review: what arrives from zoom:{account} is held" in out
    _db(lambda eng: connect_datadog(eng, tenant, rotate_secret=False))

    rows = _run(["connections", "--tenant", str(tenant)], capsys).splitlines()
    assert "STATE" in rows[0]
    at = rows[0].index("STATE")
    states = {line.split()[0]: line[at : at + 6].strip() for line in rows[1:]}
    assert states == {"datadog": "live", "zoom": "review"}

    out = _run(["connections", "review", f"zoom:{account}", "off", "--tenant", str(tenant)], capsys)
    assert "is live" in out
    out = _run(["connections", "review", "custom:push", "on", "--tenant", str(tenant)], capsys)
    rows = _run(["connections", "--tenant", str(tenant)], capsys).splitlines()
    at = rows[0].index("STATE")
    states = {line.split()[0]: line[at : at + 6].strip() for line in rows[1:]}
    assert states == {"custom": "review", "datadog": "live", "zoom": "live"}

    assert "No held records" in _run(["review", "list", "--tenant", str(tenant)], capsys)
    out = _run(["review", "release", "custom:push", "--tenant", str(tenant)], capsys)
    assert "Released 0 record(s)" in out
    with pytest.raises(SystemExit):
        cli.main(["records", "severity", str(uuid4()), "blocking", "--tenant", str(tenant)])

    async def actions(eng: AsyncEngine) -> list[str]:
        async with app_bypass_rls_context(eng) as conn:
            found = await conn.execute(
                select(AuditLogEntry.action).where(AuditLogEntry.tenant_id == tenant)
            )
            return [a for (a,) in found]

    logged = _db(actions)
    assert logged.count("connection.review_changed") == 2
    assert "review.released" in logged


def test_users_admin_and_sessions(tenant: UUID, capsys: pytest.CaptureFixture[str]) -> None:
    from checkcord.services import sessions
    from checkcord.storage.postgres import tenant_context

    argv = ["keys", "create", "--name", "k", "--user", "Dana@acme.example", "--create-user"]
    _run([*argv, "--tenant", str(tenant)], capsys)
    listed = _run(["users", "list", "--tenant", str(tenant)], capsys)
    assert "Dana@acme.example" in listed and "no" in listed

    out = _run(["users", "admin", "dana@ACME.example", "on", "--tenant", str(tenant)], capsys)
    assert "Dana@acme.example is now an admin." in out
    assert "yes" in _run(["users", "list", "--tenant", str(tenant)], capsys)

    async def sign_in(eng: AsyncEngine) -> str:
        async with tenant_context(eng, tenant) as conn:
            user_id = await conn.scalar(
                select(EnterpriseUser.id).where(EnterpriseUser.tenant_id == tenant)
            )
            assert user_id is not None
            session_id, _ = await sessions.create(conn, tenant, user_id, get_settings())
        return str(session_id)

    session_id = _db(sign_in)
    listed = _run(
        ["sessions", "list", "--user", "dana@acme.example", "--tenant", str(tenant)], capsys
    )
    assert session_id in listed
    _run(["sessions", "revoke", session_id, "--tenant", str(tenant)], capsys)
    assert session_id not in _run(["sessions", "list", "--tenant", str(tenant)], capsys)
    assert session_id in _run(["sessions", "list", "--all", "--tenant", str(tenant)], capsys)
    with pytest.raises(SystemExit) as exc:
        cli.main(["sessions", "revoke", session_id, "--tenant", str(tenant)])
    assert exc.value.code == 1

    async def is_admin(eng: AsyncEngine) -> Any:
        async with app_bypass_rls_context(eng) as conn:
            return await conn.scalar(
                select(EnterpriseUser.is_admin).where(EnterpriseUser.tenant_id == tenant)
            )

    assert _db(is_admin) is True
    _run(["users", "admin", "dana@acme.example", "off", "--tenant", str(tenant)], capsys)
    assert _db(is_admin) is False


def test_records_retire_restore_and_suggestions(
    tenant: UUID, capsys: pytest.CaptureFixture[str]
) -> None:
    old, new, suggestion = uuid4(), uuid4(), uuid4()

    async def seed(eng: AsyncEngine) -> None:
        async with app_bypass_rls_context(eng) as conn:
            await conn.execute(
                insert(CanonicalContext),
                [
                    {"id": old, "tenant_id": tenant, "summary": "Quotes run in a nightly batch."},
                    {"id": new, "tenant_id": tenant, "summary": "Quotes are fetched live."},
                ],
            )
            await conn.execute(
                insert(RecordReplacementSuggestion).values(
                    id=suggestion,
                    tenant_id=tenant,
                    record_id=old,
                    replacement_id=new,
                    basis="stated",
                )
            )

    _db(seed)
    t = ["--tenant", str(tenant)]
    out = _run(["records", "suggestions", *t], capsys)
    assert str(suggestion) in out and "nightly batch" in out and "fetched live" in out
    assert "may replace it" in _run(["records", "show", str(old), *t], capsys)
    assert "Accepted" in _run(["records", "accept", str(suggestion), *t], capsys)
    shown = _run(["records", "show", str(old), *t], capsys)
    assert "retired" in shown and str(new) in shown
    assert "No suggested replacements" in _run(["records", "suggestions", *t], capsys)
    with pytest.raises(SystemExit):
        cli.main(["records", "dismiss", str(suggestion), *t])
    with pytest.raises(SystemExit):
        cli.main(["records", "retire", str(new), "--replaced-by", str(old), *t])
    assert "Restored" in _run(["records", "restore", str(old), *t], capsys)
    out = _run(
        ["records", "retire", str(new), "--replaced-by", str(old), "--reason", "x", *t], capsys
    )
    assert "Retired" in out


def test_connections_scope(tenant: UUID, capsys: pytest.CaptureFixture[str]) -> None:
    host = f"{tenant.hex[:8]}.atlassian.net"
    key = f"jira:{host}"
    t = ["--tenant", str(tenant)]
    _db(lambda e: connect_jira(e, tenant, host, "jira-pat", False, []))

    out = _run(["connections", "scope", key, "show", *t], capsys)
    assert "include: everything" in out and "exclude: -" in out
    out = _run(["connections", "scope", key, "exclude", "PAY", "ops", *t], capsys)
    assert "exclude: pay, ops" in out and "checkcord purge source" in out
    out = _run(["connections", "scope", key, "exclude", "OPS", "--remove", *t], capsys)
    assert "exclude: pay" in out
    out = _run(["connections", "scope", key, "include", "ENG", *t], capsys)
    assert "only:    eng" in out
    assert "include: everything" in _run(["connections", "scope", key, "clear", *t], capsys)

    async def changes(eng: AsyncEngine) -> list[Any]:
        async with app_bypass_rls_context(eng) as conn:
            return list(
                (
                    await conn.execute(
                        select(AuditLogEntry.params)
                        .where(
                            (AuditLogEntry.tenant_id == tenant)
                            & (AuditLogEntry.action == "connection.scope_changed")
                        )
                        .order_by(AuditLogEntry.created_at)
                    )
                ).scalars()
            )

    logged = _db(changes)
    assert [p["exclude"] for p in logged] == [["pay", "ops"], ["pay"], ["pay"], []]
    assert logged[-1]["before"] == {"include": ["eng"], "exclude": ["pay"]}

    for argv, message in [
        (["connections", "scope", "custom:push", "show"], "have no scope"),
        (["connections", "scope", "jira:other.example.net", "include", "X"], "no connection"),
        (["connections", "scope", key, "include"], "at least one"),
        (["connections", "scope", key, "show", "X"], "takes no containers"),
    ]:
        with pytest.raises(SystemExit):
            cli.main([*argv, *t])
        assert message in capsys.readouterr().err


def test_accounts_import_list_and_unlink(
    tenant: UUID, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from checkcord.services import keys

    email = f"ana-{tenant.hex[:6]}@acme.example"
    _db(
        lambda eng: keys.create_key(
            eng, tenant, "ana", admin=False, user_email=email, create_user=True
        )
    )
    csv_file = tmp_path / "links.csv"
    csv_file.write_text(
        f"platform,account,email\nslack,U_ANA,{email.upper()}\ngithub,ghost,nobody@acme.example\n"
    )
    t = ["--tenant", str(tenant)]
    out = _run(["accounts", "import", str(csv_file), *t], capsys)
    assert "linked:          1" in out
    assert "unknown account: 1" in out
    assert "unknown person:  1" in out

    listed = _run(["accounts", "list", "--platform", "slack", *t], capsys)
    assert "U_ANA" in listed and email in listed and "manual" in listed
    assert "No unmatched accounts." in _run(["accounts", "list", "--unmatched", *t], capsys)

    out = _run(["accounts", "unlink", "slack", "U_ANA", *t], capsys)
    assert "slack:U_ANA is not matched to a person" in out
    assert "U_ANA" in _run(["accounts", "list", "--unmatched", *t], capsys)
    out = _run(["accounts", "link", "slack", "U_ANA", email, *t], capsys)
    assert f"slack:U_ANA is matched to {email}." in out


def test_connections_audience_sets_and_lists(
    tenant: UUID, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _db(lambda eng: connect_datadog(eng, tenant, rotate_secret=False))
    key = f"datadog:{result.installation_id}"
    t = ["--tenant", str(tenant)]
    rows = _run(["connections", *t], capsys).splitlines()
    at = rows[0].index("AUDIENCE")
    assert rows[1][at:].startswith("nobody")

    out = _run(["connections", "audience", key, "--everyone", *t], capsys)
    assert f"Audience of {key} set." in out
    rows = _run(["connections", *t], capsys).splitlines()
    assert rows[1][at:].startswith("everyone")

    with pytest.raises(SystemExit):
        cli.main(["connections", "audience", key, "--people", "nobody@acme.example", *t])
    assert "no person nobody@acme.example" in capsys.readouterr().err
