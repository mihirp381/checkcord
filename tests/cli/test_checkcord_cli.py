import argparse
import asyncio
import base64
import re
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

import checkcord.cli.commands as cli
from checkcord.adapters.code.providers.github import connector as github
from checkcord.adapters.meetings.providers.zoom import connector as zoom
from checkcord.adapters.monitoring.providers.datadog import connector as datadog
from checkcord.adapters.monitoring.providers.sentry import connector as sentry
from checkcord.adapters.tasks.providers.jira import connector as jira
from checkcord.connectors import cli as setup_cli
from checkcord.connectors.cli import ConnectResult
from checkcord.core import startup
from checkcord.core.config import Settings
from checkcord.core.crypto import hash_api_key
from checkcord.core.startup import StartupConfigError
from checkcord.services import keys

TENANT = UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    s = Settings(environment="test", default_tenant_id=None, public_base_url=None)
    monkeypatch.setattr(cli, "_settings", lambda: s)
    return s


def test_token_format() -> None:
    token = keys.generate_token()
    assert token.startswith("chk_")
    body = token.removeprefix("chk_")
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", body)
    assert len(base64.urlsafe_b64decode(body + "=")) == 32
    assert keys.generate_token() != token


def test_tokens_are_stored_as_sha256() -> None:
    import hashlib

    token = keys.generate_token()
    assert hash_api_key(token) == hashlib.sha256(token.encode()).hexdigest()


@pytest.mark.parametrize(
    ("argv", "handler"),
    [
        (["api"], cli.cmd_api),
        (["worker"], cli.cmd_worker),
        (["mcp"], cli.cmd_mcp),
        (["migrate"], cli.cmd_migrate),
        (["init"], cli.cmd_init),
        (["demo", "load"], cli.cmd_demo_load),
        (["demo", "remove"], cli.cmd_demo_remove),
        (["keys", "create", "--name", "ci"], cli.cmd_keys_create),
        (["keys", "list"], cli.cmd_keys_list),
        (["keys", "revoke", str(TENANT)], cli.cmd_keys_revoke),
        (["connect", "github", "--repo", "o/r"], cli.cmd_connect_registered),
        (["connect", "jira", "--host", "x.atlassian.net"], cli.cmd_connect_registered),
        (["connect", "datadog"], cli.cmd_connect_registered),
        (["connect", "webhook", "--name", "status-page"], cli.cmd_connect_registered),
        (["connect", "sentry"], cli.cmd_connect_registered),
        (["connect", "zoom", "--account-id", "abc"], cli.cmd_connect_registered),
        (["connect", "teams-meetings", "--ms-tenant-id", "t"], cli.cmd_connect_registered),
        (["connect", "slack"], cli.cmd_connect_oauth),
        (["connect", "teams"], cli.cmd_connect_oauth),
        (["connect", "notion"], cli.cmd_connect_oauth),
        (["connect", "linear"], cli.cmd_connect_oauth),
        (["disconnect", "slack", "T1"], cli.cmd_disconnect),
        (["secrets", "encrypt"], cli.cmd_secrets_encrypt),
        (["connections"], cli.cmd_connections),
        (["connections", "review", "jira:x.atlassian.net", "on"], cli.cmd_connections_review),
        (["connections", "scope", "jira:x.atlassian.net", "show"], cli.cmd_connections_scope),
        (["connections", "scope", "slack:T1", "exclude", "C1", "C2"], cli.cmd_connections_scope),
        (["connections", "scope", "x:T", "include", "C", "--remove"], cli.cmd_connections_scope),
        (["review", "list"], cli.cmd_review_list),
        (["review", "list", "--connection", "custom:push"], cli.cmd_review_list),
        (["review", "release", "github:o/r"], cli.cmd_review_release),
        (["review", "release", "--record", str(TENANT)], cli.cmd_review_release),
        (["review", "reject", "github:o/r", "--yes"], cli.cmd_review_reject),
        (["records", "show", str(TENANT)], cli.cmd_records_show),
        (["records", "severity", str(TENANT), "blocking"], cli.cmd_records_severity),
        (["records", "retire", str(TENANT), "--replaced-by", str(TENANT)], cli.cmd_records_retire),
        (["records", "restore", str(TENANT)], cli.cmd_records_restore),
        (["records", "suggestions"], cli.cmd_records_suggestions),
        (["records", "accept", str(TENANT)], cli.cmd_records_decide),
        (["records", "dismiss", str(TENANT)], cli.cmd_records_decide),
        (["doctor"], cli.cmd_doctor),
        (["users", "list", "--all"], cli.cmd_users_list),
        (["users", "admin", "a@x.example", "on"], cli.cmd_users_admin),
        (["sessions", "list", "--user", "a@x.example"], cli.cmd_sessions_list),
        (["sessions", "revoke", str(TENANT)], cli.cmd_sessions_revoke),
    ],
)
def test_commands_dispatch(argv: list[str], handler: object) -> None:
    assert cli.build_parser().parse_args(argv).handler is handler


def test_api_defaults() -> None:
    args = cli.build_parser().parse_args(["api"])
    assert (args.host, args.port) == ("127.0.0.1", 8000)
    args = cli.build_parser().parse_args(["api", "--host", "0.0.0.0", "--port", "9000"])
    assert (args.host, args.port) == ("0.0.0.0", 9000)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["keys"],
        ["keys", "create"],
        ["keys", "revoke", "not-a-uuid"],
        ["init", "--tenant", "nope"],
        ["connect"],
        ["connect", "discord"],
        ["connect", "github"],
        ["disconnect"],
        ["disconnect", "slack"],
        ["disconnect", "myspace", "x"],
        ["secrets"],
        ["connect", "zoom"],
        ["api", "--port", "x"],
        ["connections", "review", "github:o/r"],
        ["connections", "review", "github:o/r", "maybe"],
        ["connections", "scope", "github:o/r"],
        ["connections", "scope", "github:o/r", "only", "o/r"],
        ["review"],
        ["records", "severity", str(TENANT), "high"],
        ["records", "show", "not-a-uuid"],
        ["records", "retire", str(TENANT), "--replaced-by", "nope"],
        ["records", "accept"],
        ["connect", "slack", "--review"],
    ],
)
def test_bad_arguments_exit_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["init"],
        ["keys", "create", "--name", "n"],
        ["keys", "list"],
        ["keys", "revoke", str(uuid4())],
        ["connect", "datadog"],
        ["connect", "sentry"],
        ["connect", "jira", "--host", "h"],
        ["connect", "zoom", "--account-id", "a"],
        ["connect", "teams-meetings", "--ms-tenant-id", "t"],
        ["connect", "github", "--repo", "o/r"],
        ["connect", "slack"],
        ["connect", "teams"],
        ["connect", "notion"],
        ["connect", "linear"],
        ["disconnect", "teams", "t"],
        ["connections"],
        ["connections", "review", "custom:push", "off"],
        ["connections", "scope", "slack:T1", "clear"],
        ["review", "list"],
        ["review", "release", "custom:push"],
        ["review", "reject", "custom:push"],
        ["records", "show", str(uuid4())],
        ["records", "severity", str(uuid4()), "advisory"],
        ["records", "retire", str(uuid4()), "--reason", "gone"],
        ["records", "restore", str(uuid4())],
        ["records", "suggestions"],
        ["records", "dismiss", str(uuid4())],
    ],
)
def test_tenant_writing_commands_take_tenant(argv: list[str]) -> None:
    args = cli.build_parser().parse_args([*argv, "--tenant", str(TENANT)])
    assert args.tenant == TENANT


def test_tenant_falls_back_to_default(settings: Settings) -> None:
    settings.default_tenant_id = TENANT
    assert cli._tenant(argparse.Namespace(tenant=None)) == TENANT
    other = uuid4()
    assert cli._tenant(argparse.Namespace(tenant=other)) == other


def test_missing_tenant_is_a_clear_error(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["init"])
    assert exc.value.code == 1
    assert "pass --tenant UUID or set DEFAULT_TENANT_ID" in capsys.readouterr().err


def test_public_url_required(settings: Settings) -> None:
    with pytest.raises(cli.CLIError, match="PUBLIC_BASE_URL"):
        cli._public_url(argparse.Namespace(public_url=None))
    settings.public_base_url = "https://checkcord.example.com/"
    assert cli._public_url(argparse.Namespace(public_url=None)) == "https://checkcord.example.com"


def test_secret_from_option_then_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECKCORD_TEST_SECRET", " from-env ")
    assert setup_cli.read_secret("from-option", "CHECKCORD_TEST_SECRET", "Secret") == "from-option"
    assert setup_cli.read_secret(None, "CHECKCORD_TEST_SECRET", "Secret") == "from-env"


def test_secret_prompt_is_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHECKCORD_TEST_SECRET", raising=False)
    monkeypatch.setattr(setup_cli.sys.stdin, "isatty", lambda: True)
    getpass = MagicMock(return_value="typed")
    monkeypatch.setattr(setup_cli.getpass, "getpass", getpass)
    assert setup_cli.read_secret(None, "CHECKCORD_TEST_SECRET", "Secret") == "typed"
    getpass.assert_called_once_with("Secret: ")


def test_secret_without_tty_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHECKCORD_TEST_SECRET", raising=False)
    monkeypatch.setattr(setup_cli.sys.stdin, "isatty", lambda: False)
    with pytest.raises(ValueError, match="CHECKCORD_TEST_SECRET"):
        setup_cli.read_secret(None, "CHECKCORD_TEST_SECRET", "Secret")


def test_supplied_secrets_are_not_echoed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.default_tenant_id = TENANT
    installation_id = uuid4()
    result = AsyncMock(return_value=ConnectResult(installation_id, True, None))
    monkeypatch.setattr(sentry, "connect_sentry", result)
    monkeypatch.setattr(zoom, "connect_zoom", result)
    monkeypatch.setattr(cli, "_with_engine", lambda fn: asyncio.run(fn(object())))
    cli.main(
        [
            "connect",
            "sentry",
            "--client-secret",
            "sentry-secret-value",
            "--public-url",
            "https://l.example",
        ]
    )
    cli.main(
        [
            "connect",
            "zoom",
            "--account-id",
            "acct",
            "--secret-token",
            "zoom-secret-value",
            "--public-url",
            "https://l.example",
        ]
    )
    out = capsys.readouterr().out
    assert "sentry-secret-value" not in out and "zoom-secret-value" not in out
    assert f"https://l.example/api/v1/webhooks/sentry/{installation_id}" in out
    assert "https://l.example/api/v1/webhooks/zoom?zoom_account_id=acct" in out


def test_generated_secret_shown_once(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.default_tenant_id = TENANT
    results = iter(
        [
            ConnectResult(uuid4(), True, "generated-once"),
            ConnectResult(uuid4(), False, None),
        ]
    )

    async def fake_connect(*_: object) -> ConnectResult:
        return next(results)

    monkeypatch.setattr(datadog, "connect_datadog", fake_connect)
    monkeypatch.setattr(cli, "_with_engine", lambda fn: asyncio.run(fn(object())))
    cli.main(["connect", "datadog", "--public-url", "https://l.example"])
    first = capsys.readouterr().out
    cli.main(["connect", "datadog", "--public-url", "https://l.example"])
    second = capsys.readouterr().out
    assert first.count("generated-once") == 1
    assert "generated-once" not in second and "--rotate-secret" in second


def test_revoke_mentions_cache_ttl(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.default_tenant_id = TENANT
    monkeypatch.setattr(cli, "_with_engine", lambda fn: True)
    cli.main(["keys", "revoke", str(uuid4())])
    assert "300 seconds" in capsys.readouterr().out


def test_revoke_unknown_key_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.default_tenant_id = TENANT
    monkeypatch.setattr(cli, "_with_engine", lambda fn: False)
    with pytest.raises(SystemExit) as exc:
        cli.main(["keys", "revoke", str(uuid4())])
    assert exc.value.code == 1


def test_github_repo_must_be_owner_name(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.default_tenant_id = TENANT
    with pytest.raises(SystemExit):
        cli.main(["connect", "github", "--repo", "just-a-name", "--public-url", "https://x"])
    assert "OWNER/NAME" in capsys.readouterr().err


def test_doctor_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from checkcord.services import doctor

    checks = [doctor.Check("database", True, "reachable"), doctor.Check("redis", False, "down")]
    monkeypatch.setattr(doctor, "run_checks", AsyncMock(return_value=checks))
    monkeypatch.setattr(cli, "_settings", lambda: Settings(environment="test"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["doctor"])
    assert exc.value.code == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("PASS  database") and lines[1].startswith("FAIL  redis")


@pytest.mark.parametrize(
    ("environment", "password", "ok"),
    [
        ("production", "checkcord_app", False),
        ("production", "checkcord", False),
        ("production", "s3cret-pw", True),
        ("development", "checkcord_app", True),
    ],
)
def test_doctor_flags_shipped_database_password_in_production(
    environment: str, password: str, ok: bool
) -> None:
    from checkcord.services import doctor

    settings = Settings(
        environment=environment,
        database_url=f"postgresql+asyncpg://checkcord_app:{password}@db:5432/checkcord",
    )
    check = next(c for c in doctor._key_checks(settings) if c.name == "db password")
    assert check.ok is ok
    assert ("shipped default password" in check.detail) is not ok


@pytest.mark.parametrize(
    ("model", "ok"), [("text-embedding-ada-002", False), ("text-embedding-3-small", True)]
)
def test_doctor_flags_embedding_model_that_cannot_fill_the_column(model: str, ok: bool) -> None:
    from checkcord.services import doctor

    settings = Settings(embedding_provider="openai", embedding_model=model)
    check = next(c for c in doctor._key_checks(settings) if c.name == "embedding model")
    assert check.ok is ok
    assert ("always returns 1536" in check.detail) is not ok


class _FakeConn:
    def __init__(self, rows: list[list[tuple[str | None, ...]]]) -> None:
        self._rows = iter(rows)

    async def execute(self, stmt: object) -> list[tuple[str | None, ...]]:
        return next(self._rows)


def _patch_credential_rows(
    monkeypatch: pytest.MonkeyPatch, rows: list[list[tuple[str | None, ...]]]
) -> None:
    from contextlib import asynccontextmanager

    from checkcord.cli import secrets
    from checkcord.services import doctor
    from checkcord.storage.models import GithubInstallation, ZoomInstallation

    @asynccontextmanager
    async def fake_context(engine: object):  # type: ignore[no-untyped-def]
        yield _FakeConn(rows)

    monkeypatch.setattr(doctor, "app_bypass_rls_context", fake_context)
    monkeypatch.setattr(
        secrets,
        "SECRET_COLUMNS",
        [
            (ZoomInstallation, ("webhook_secret",)),
            (GithubInstallation, ("personal_access_token", "webhook_secret")),
        ],
    )


@pytest.mark.asyncio
async def test_doctor_credential_check_fails_on_undecryptable_ciphertext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cryptography.fernet import Fernet

    from checkcord.services import doctor

    foreign = Fernet(Fernet.generate_key()).encrypt(b"ghp_x").decode()
    _patch_credential_rows(monkeypatch, [[("legacy-plaintext",)], [(None, foreign)]])
    check = await doctor.credential_check(MagicMock())
    assert check.name == "stored credentials" and not check.ok
    assert "FERNET_MASTER_KEY" in check.detail


@pytest.mark.asyncio
async def test_doctor_credential_check_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    from checkcord.core.crypto import encrypt_secret
    from checkcord.services import doctor

    _patch_credential_rows(monkeypatch, [[(encrypt_secret("zoom"),)]])
    check = await doctor.credential_check(MagicMock())
    assert check.ok and "zoom_installations" in check.detail


@pytest.mark.asyncio
async def test_doctor_credential_check_with_nothing_encrypted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from checkcord.services import doctor

    _patch_credential_rows(monkeypatch, [[("plain",)], []])
    check = await doctor.credential_check(MagicMock())
    assert check.ok and check.detail == "none encrypted yet"


def test_keys_create_user_flags_parse() -> None:
    args = cli.build_parser().parse_args(
        ["keys", "create", "--name", "n", "--user", "a@x.example", "--create-user"]
    )
    assert (args.user, args.create_user) == ("a@x.example", True)


def test_create_user_needs_user(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["keys", "create", "--name", "n", "--create-user", "--tenant", str(TENANT)])
    assert exc.value.code == 1
    assert "--user" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("raw", "host"),
    [
        ("acme.atlassian.net", "acme.atlassian.net"),
        ("https://Acme.Atlassian.net/", "acme.atlassian.net"),
        ("https://jira.acme.com:8443/jira", "jira.acme.com:8443"),
    ],
)
def test_normalize_jira_host(raw: str, host: str) -> None:
    assert jira.normalize_jira_host(raw) == host


def test_jira_http_host_only_in_development() -> None:
    assert jira.normalize_jira_host("http://Stand-In:8900/", allow_http=True) == (
        "http://stand-in:8900"
    )
    with pytest.raises(ValueError, match="https"):
        jira.normalize_jira_host("http://stand-in:8900", allow_http=False)
    with pytest.raises(ValueError):
        jira.normalize_jira_host("ftp://stand-in")


def test_webhook_urls() -> None:
    base = "https://checkcord.example.com/"
    iid = uuid4()
    assert jira.webhook_url(base) == "https://checkcord.example.com/api/v1/webhooks/jira"
    assert datadog.webhook_url(base, iid).endswith(f"/datadog/{iid}")
    assert sentry.webhook_url(base, iid).endswith(f"/sentry/{iid}")
    assert zoom.webhook_url(base, "a b").endswith("zoom_account_id=a%20b")
    assert github.webhook_url(base).endswith("/api/v1/webhooks/github")


def test_help_tree_renders(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for command in ("api", "worker", "migrate", "init", "keys", "connect", "doctor", "proxy"):
        assert command in out


@pytest.mark.parametrize(
    ("superuser", "bypassrls", "problem"),
    [(False, False, False), (True, False, True), (False, True, True), (True, True, True)],
)
def test_database_role_problem(superuser: bool, bypassrls: bool, problem: bool) -> None:
    message = startup.database_role_problem("checkcord_app", superuser, bypassrls)
    assert (message is not None) is problem
    if message:
        assert "checkcord_app" in message and "row-level security" in message


async def _flags(*_: object) -> tuple[str, bool, bool]:
    return ("checkcord", True, False)


@pytest.mark.asyncio
async def test_rls_exempt_role_is_fatal_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(startup, "database_role_flags", _flags)
    with pytest.raises(StartupConfigError, match="SUPERUSER"):
        await startup.enforce_database_role(Settings(environment="production"), MagicMock())


@pytest.mark.asyncio
async def test_rls_exempt_role_warns_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(startup, "database_role_flags", _flags)
    logger = MagicMock()
    monkeypatch.setattr(startup, "logger", logger)
    await startup.enforce_database_role(Settings(environment="development"), MagicMock())
    logger.warning.assert_called_once()
    assert logger.warning.call_args.args[0] == "startup.DATABASE_ROLE_BYPASSES_RLS"


@pytest.mark.parametrize("provider", ["slack", "teams", "notion", "linear"])
def test_connect_oauth_prints_signed_install_link(
    settings: Settings, provider: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from urllib.parse import unquote

    from checkcord.core.oauth_state import INSTALL_PATHS, read_state

    prefix = {"slack": "slack", "teams": "msteams", "notion": "notion", "linear": "linear"}
    setattr(settings, f"{prefix[provider]}_client_id", "client-id")
    setattr(settings, f"{prefix[provider]}_redirect_uri", f"https://l.example/{provider}/cb")
    cli.main(["connect", provider, "--tenant", str(TENANT), "--public-url", "https://l.example/"])
    out = capsys.readouterr().out
    match = re.search(r"^\s+(https://l\.example\S+)$", out, re.M)
    assert match, out
    url = match.group(1)
    head, state = url.split("?state=")
    assert head == f"https://l.example{INSTALL_PATHS[provider]}"
    assert read_state(unquote(state), provider).tenant_id == TENANT
    assert "15 minutes" in out and f"https://l.example/{provider}/cb" in out


def test_connect_slack_prints_app_scopes_and_events(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    from checkcord.core.config import SLACK_BOT_EVENTS

    settings.slack_client_id = "client-id"
    settings.slack_redirect_uri = "https://l.example/slack/cb"
    settings.slack_bot_scopes = ["chat:write", "channels:history"]
    cli.main(["connect", "slack", "--tenant", str(TENANT), "--public-url", "https://l.example"])
    out = capsys.readouterr().out
    assert "Bot token scopes: chat:write, channels:history" in out
    assert f"Bot events:       {', '.join(SLACK_BOT_EVENTS)}" in out


def test_connect_non_slack_omits_slack_scopes(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.notion_client_id = "client-id"
    settings.notion_redirect_uri = "https://l.example/notion/cb"
    cli.main(["connect", "notion", "--tenant", str(TENANT), "--public-url", "https://l.example"])
    assert "scopes" not in capsys.readouterr().out


def test_connect_oauth_requires_app_config(settings: Settings) -> None:
    with pytest.raises(cli.CLIError, match="SLACK_CLIENT_ID"):
        cli.cmd_connect_oauth(
            argparse.Namespace(provider="slack", tenant=TENANT, public_url="https://l.example")
        )


def test_disconnect_reports_missing(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_with_engine", lambda fn: False)
    with pytest.raises(cli.CLIError, match="no slack connection"):
        cli.cmd_disconnect(
            argparse.Namespace(
                provider="slack", identifier="T1", tenant=TENANT, purge=False, yes=False
            )
        )


def test_doctor_warn_does_not_fail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from checkcord.services import doctor

    checks = [
        doctor.Check("database", True, "reachable"),
        doctor.Check("dead letters", True, "2 in x.dlq; run `checkcord dlq list`", warn=True),
    ]
    monkeypatch.setattr(doctor, "run_checks", AsyncMock(return_value=checks))
    monkeypatch.setattr(cli, "_settings", lambda: Settings(environment="test"))
    cli.main(["doctor"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith("WARN  dead letters") and "checkcord dlq list" in lines[1]


@pytest.mark.asyncio
async def test_dead_letter_check_names_the_command() -> None:
    import fakeredis.aioredis

    from checkcord.bus.dead_letter import write_dead_letter
    from checkcord.services import doctor

    redis = fakeredis.aioredis.FakeRedis()
    assert (await doctor.dead_letter_check(redis)).status == "PASS"
    await write_dead_letter(redis, "events.raw.custom", {"a": "1"}, "boom")
    check = await doctor.dead_letter_check(redis)
    assert check.status == "WARN"
    assert "events.raw.custom.dlq" in check.detail and "checkcord dlq list" in check.detail


@pytest.mark.parametrize(
    "argv",
    [
        ["events", "retry"],
        ["events", "retry", "--all", "--id", str(uuid4())],
        ["dlq", "replay", "x.dlq"],
        ["dlq", "drop", "x.dlq", "--all", "--id", "1-0"],
    ],
)
def test_bulk_commands_need_exactly_one_selector(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2


def _fake_redis(monkeypatch: pytest.MonkeyPatch) -> Any:
    import asyncio

    import fakeredis.aioredis

    redis = fakeredis.aioredis.FakeRedis()

    def run(fn: Any) -> Any:
        return asyncio.run(fn(redis))

    monkeypatch.setattr(cli, "_with_redis", run)
    return redis


def test_dlq_show_hides_content_and_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio
    import json

    from checkcord.bus.dead_letter import write_dead_letter

    redis = _fake_redis(monkeypatch)
    fake_key = "sk-" + "Zz9Yy8Xx" * 4
    payload = {"event_id": "e-1", "content_clean": "private words", "token": fake_key}
    asyncio.run(
        write_dead_letter(
            redis,
            "events.raw.custom",
            {"data": json.dumps(payload)},
            f"Traceback\nKeyError: {fake_key}",
        )
    )
    cli.main(["dlq", "show", "events.raw.custom.dlq"])
    out = capsys.readouterr().out
    assert "KeyError: [REDACTED:TOKEN]" in out
    assert fake_key not in out and "private words" not in out


def test_dlq_refuses_a_live_stream(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_redis(monkeypatch)
    monkeypatch.setattr(cli, "_record", record := MagicMock())
    with pytest.raises(SystemExit) as exc:
        cli.main(["dlq", "drop", "events.raw.custom", "--all", "--tenant", str(uuid4())])
    assert exc.value.code == 1
    assert "not a dead-letter stream" in capsys.readouterr().err
    record.assert_not_called()


def test_dlq_replay_reports_unknown_ids_and_records_the_replay(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_redis(monkeypatch)
    monkeypatch.setattr(cli, "_record", record := MagicMock())
    tenant = uuid4()
    with pytest.raises(SystemExit) as exc:
        cli.main(["dlq", "replay", "events.raw.custom.dlq", "--id", "1-0", "--tenant", str(tenant)])
    assert exc.value.code == 1
    assert "1-0: no such entry" in capsys.readouterr().out
    record.assert_called_once()
    assert record.call_args.args == (tenant, "dlq.replay")
    assert record.call_args.kwargs["params"]["stream"] == "events.raw.custom.dlq"
    assert record.call_args.kwargs["record_ids"] == []


def test_dlq_drop_needs_a_tenant_for_the_audit_entry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    redis = _fake_redis(monkeypatch)
    monkeypatch.setattr(cli, "_settings", lambda: MagicMock(default_tenant_id=None))
    drop = AsyncMock()
    monkeypatch.setattr("checkcord.bus.dead_letter.drop", drop)
    with pytest.raises(cli.CLIError, match="no tenant"):
        cli.cmd_dlq_drop(argparse.Namespace(stream="x.dlq", all=True, ids=None, tenant=None))
    drop.assert_not_awaited()
    assert redis is not None


def test_doctor_prints_retention() -> None:
    from checkcord.services import doctor

    assert doctor._retention_check(Settings(environment="test")).detail == (
        "events forever, audit entries forever"
    )
    check = doctor._retention_check(
        Settings(environment="test", event_retention_days=90, audit_retention_days=365)
    )
    assert check.ok and check.detail == "events 90 days, audit entries 365 days"


def test_audit_prune_needs_a_retention(settings: Settings) -> None:
    with pytest.raises(cli.CLIError, match="AUDIT_RETENTION_DAYS"):
        cli.cmd_audit_prune(argparse.Namespace(days=None, dry_run=True, yes=False))


def test_purge_asks_before_deleting(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from checkcord.services import purge

    calls: list[str] = []
    monkeypatch.setattr(cli, "_run_purge", lambda *a: calls.append("purged"))
    monkeypatch.setattr(cli, "_with_engine", lambda fn: purge.Purged(events=2))
    argv = ["purge", "event", "--id", str(uuid4()), "--tenant", str(TENANT)]

    cli.main([*argv, "--dry-run"])
    assert "events:" in capsys.readouterr().out and calls == []
    monkeypatch.setattr("builtins.input", lambda _: "")
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert calls == []
    cli.main([*argv, "--yes"])
    assert calls == ["purged"]


@pytest.mark.parametrize(
    "argv",
    [
        ["connect", "github", "--repo", "o/r"],
        ["connect", "jira", "--host", "h"],
        ["connect", "datadog"],
        ["connect", "sentry"],
        ["connect", "zoom", "--account-id", "a"],
        ["connect", "teams-meetings", "--ms-tenant-id", "t"],
    ],
)
def test_connect_takes_review(argv: list[str]) -> None:
    assert cli.build_parser().parse_args(argv).review is False
    assert cli.build_parser().parse_args([*argv, "--review"]).review is True


@pytest.mark.parametrize("value", ["push", ":x", "custom:", "custom:  "])
def test_connection_must_be_provider_and_identifier(value: str) -> None:
    with pytest.raises(cli.CLIError):
        cli._connection(value)


def test_connection_identifier_is_trimmed() -> None:
    assert cli._connection("jira: x.atlassian.net ") == "jira:x.atlassian.net"


def test_review_needs_one_target(settings: Settings) -> None:
    for argv in (["review", "release"], ["review", "release", "a:b", "--record", str(TENANT)]):
        args = cli.build_parser().parse_args([*argv, "--tenant", str(TENANT)])
        with pytest.raises(cli.CLIError, match="CONNECTION or --record"):
            args.handler(args)


@pytest.mark.asyncio
async def test_doctor_review_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import asynccontextmanager

    from checkcord.services import doctor, review

    tenants = [uuid4(), uuid4()]
    counts = iter([{"jira:a": 2, "github:o/r": 0}, {"jira:a": 1}])

    @asynccontextmanager
    async def fake_context(*_args: object, **kwargs: object) -> AsyncIterator[object]:
        assert kwargs == {"include_held": True}
        yield MagicMock()

    async def fake_counts(_conn: object, _tenant: object) -> dict[str, int]:
        return next(counts)

    monkeypatch.setattr(doctor, "list_tenant_ids", AsyncMock(return_value=tenants))
    monkeypatch.setattr(doctor, "tenant_context", fake_context)
    monkeypatch.setattr(review, "held_counts", fake_counts)
    check = await doctor.review_check(MagicMock())
    assert check is not None and check.warn and check.ok
    assert check.detail.startswith("3 connection(s) in review or holding material, 3 held")

    monkeypatch.setattr(doctor, "list_tenant_ids", AsyncMock(return_value=[]))
    assert await doctor.review_check(MagicMock()) is None


def test_log_lines_go_to_stderr(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    import structlog

    def run(fn: Any) -> list[Any]:
        structlog.get_logger("checkcord.audit").info(
            "audit", logger="checkcord.audit", action="keys.list"
        )
        structlog.get_logger("checkcord.llm").info("model_client_started")
        return []

    monkeypatch.setattr(cli, "_with_engine", run)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    cli.main(["keys", "list", "--tenant", str(TENANT), "--verbose"])
    out, err = capsys.readouterr()
    assert out.strip() == f"No API keys for tenant {TENANT}."
    for line in out.splitlines():
        with pytest.raises(ValueError):
            json.loads(line)
    assert '"action": "keys.list"' in err
    assert "model_client_started" in err


@pytest.mark.parametrize(
    ("argv", "env", "shown"),
    [
        (["keys", "list"], None, False),
        (["keys", "list"], "WARNING", False),
        (["keys", "list"], "info", True),
        (["keys", "list"], "debug", True),
        (["--verbose", "keys", "list"], None, True),
        (["keys", "list", "--verbose"], None, True),
    ],
)
def test_admin_commands_print_info_lines_only_when_asked(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    env: str | None,
    shown: bool,
) -> None:
    import structlog

    def run(fn: Any) -> list[Any]:
        structlog.get_logger("checkcord.audit").info("audit", action="keys.list")
        structlog.get_logger("checkcord.llm").warning("model_slow")
        return []

    monkeypatch.setattr(cli, "_with_engine", run)
    if env is None:
        monkeypatch.delenv("LOG_LEVEL", raising=False)
    else:
        monkeypatch.setenv("LOG_LEVEL", env)
    cli.main([*argv, "--tenant", str(TENANT)])
    err = capsys.readouterr().err
    assert ('"action": "keys.list"' in err) is shown
    assert "model_slow" in err


def test_servers_keep_their_own_log_level() -> None:
    import argparse

    assert cli.log_level(argparse.Namespace(command="api", verbose=False)) == 0
    assert cli.log_level(argparse.Namespace(command="worker", verbose=False)) == 0


@pytest.fixture
def bare_logging(monkeypatch: pytest.MonkeyPatch) -> Any:
    import logging

    import structlog

    root = logging.getLogger()
    level, handlers, config = root.level, root.handlers, structlog.get_config()
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    def fresh() -> None:
        root.handlers = []
        root.setLevel(logging.WARNING)

    yield fresh
    root.handlers = handlers
    root.setLevel(level)
    structlog.reset_defaults()
    structlog.configure(**config)


def test_worker_entry_point_logs_info_by_default(
    settings: Settings,
    bare_logging: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    import structlog

    from checkcord import worker

    async def fake_main(heartbeat_file: Any) -> None:
        structlog.get_logger("checkcord.storage.scheduled_jobs").info("scheduled_job.started")

    monkeypatch.setattr(worker, "main", fake_main)
    bare_logging()
    cli.main(["worker"])
    assert _logged("scheduled_job.started", capsys, caplog)


def test_api_entry_point_logs_info_by_default(
    settings: Settings,
    bare_logging: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    import uvicorn
    from fastapi import FastAPI

    from checkcord import main as api

    async def serve(*args: Any, **kwargs: Any) -> None:
        async with api.lifespan(FastAPI()):
            pass

    monkeypatch.setattr(api, "app_lifecycle", MagicMock(side_effect=RuntimeError("stop")))
    monkeypatch.setattr(api, "validate_security_settings", lambda s: None)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: asyncio.run(_swallow(serve())))
    bare_logging()
    cli.main(["api"])
    assert _logged("api_starting", capsys, caplog)


def _logged(event: str, capsys: Any, caplog: Any) -> bool:
    return event in capsys.readouterr().err or any(event in r.getMessage() for r in caplog.records)


async def _swallow(coro: Any) -> None:
    try:
        await coro
    except RuntimeError:
        pass


def test_admin_command_stays_quiet_on_bare_logging(
    settings: Settings,
    bare_logging: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    import structlog

    def run(fn: Any) -> list[Any]:
        structlog.get_logger("checkcord.audit").info("audit", action="keys.list")
        return []

    monkeypatch.setattr(cli, "_with_engine", run)
    bare_logging()
    cli.main(["keys", "list", "--tenant", str(TENANT)])
    assert not _logged("keys.list", capsys, caplog)
