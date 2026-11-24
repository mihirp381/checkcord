from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from checkcord.adapters.chat.providers.slack.audience import read_slack_channel
from checkcord.adapters.code.providers.github.parser import read_github_repository
from checkcord.services import audiences
from checkcord.storage import audiences as stored
from checkcord.storage.audiences import EMPTY, Applied, Audience

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
HOUR = timedelta(hours=1)


def test_within_keeps_only_readers_of_both() -> None:
    a, b, c = (str(uuid4()) for _ in range(3))
    assert Audience(False, frozenset({a, b})).within(Audience(False, frozenset({b, c}))) == (
        Audience(False, frozenset({b}))
    )
    assert Audience(True).within(Audience(False, frozenset({a}))) == Audience(False, frozenset({a}))
    assert Audience(False, frozenset({a})).within(Audience(True)) == Audience(False, frozenset({a}))


def test_private_access_control_has_no_public_flag() -> None:
    ac = Audience.of(None, [uuid4()]).access_control("github", "o/r")
    assert ac.is_public is False and ac.acl_type.value == "repo_collaborators"
    assert Audience.of(True, [uuid4()]).access_control("slack", "C1").allowed_identity_ids == []


@pytest.mark.parametrize(
    ("attempted", "refreshed", "fresh"),
    [
        (None, None, False),
        (NOW - 2 * HOUR, NOW - 2 * HOUR, False),
        (NOW - timedelta(minutes=5), NOW - timedelta(minutes=5), True),
        (NOW - timedelta(minutes=5), None, True),
        (NOW - timedelta(minutes=5), NOW - 30 * HOUR, False),
    ],
)
def test_is_fresh(attempted: Any, refreshed: Any, fresh: bool) -> None:
    row = {"attempted_at": attempted, "refreshed_at": refreshed}
    assert stored.is_fresh(row, NOW, HOUR, 24 * HOUR) is fresh


def test_narrowed() -> None:
    a, b = str(uuid4()), str(uuid4())
    assert stored._narrowed({"is_public": True}, {"is_public": False})
    assert stored._narrowed({"allowed_identity_ids": [a, b]}, {"allowed_identity_ids": [a]})
    assert not stored._narrowed({"allowed_identity_ids": [a]}, {"allowed_identity_ids": [a, b]})
    assert not stored._narrowed({"is_public": False}, {"is_public": True})


@pytest.mark.asyncio
async def test_slack_reader_pages_and_fails_closed() -> None:
    client = AsyncMock()
    client.conversations_info.return_value = {"ok": True, "channel": {"is_private": True}}
    client.conversations_members.side_effect = [
        {"ok": True, "members": ["U1"], "response_metadata": {"next_cursor": "n"}},
        {"ok": True, "members": ["U2"]},
    ]
    assert await read_slack_channel(client, "C1") == audiences.Reading(False, ("U1", "U2"))

    client.conversations_info.return_value = {"ok": False, "error": "channel_not_found"}
    with pytest.raises(audiences.ReadFailed):
        await read_slack_channel(client, "C1")
    client.conversations_info.return_value = {"ok": True, "channel": {}}
    client.conversations_members.side_effect = None
    client.conversations_members.return_value = {"ok": True, "members": []}
    with pytest.raises(audiences.ReadFailed):
        await read_slack_channel(client, "C1")


@pytest.mark.asyncio
async def test_github_reader_treats_unknown_visibility_as_private() -> None:
    client = MagicMock()
    client.get_repository = AsyncMock(return_value={"visibility": "internal"})
    client.get_collaborators = AsyncMock(return_value=["alice"])
    assert await read_github_repository(client, "o/r") == audiences.Reading(False, ("alice",))

    client.get_repository.return_value = None
    with pytest.raises(audiences.ReadFailed):
        await read_github_repository(client, "o/r")
    client.get_collaborators.return_value = []
    with pytest.raises(audiences.ReadFailed):
        await read_github_repository(client, "o/r", private=True)


@pytest.mark.asyncio
async def test_refresh_audits_counts_without_members() -> None:
    a, b = str(uuid4()), str(uuid4())
    applied = Applied(
        Audience(False, frozenset({a})), Audience(False, frozenset({b})), events=3, records=2
    )
    applied.narrowed_records.append(uuid4())
    record = AsyncMock()
    resolved = AsyncMock(return_value=({"U1": uuid4()}, ["U9"]))
    with (
        patch.object(stored, "apply", AsyncMock(return_value=applied)) as apply,
        patch.object(audiences, "_record", record),
        patch.object(audiences, "rewrite_files", AsyncMock()) as rewrite,
        patch("checkcord.services.accounts.match", resolved),
    ):

        async def reader() -> audiences.Reading:
            return audiences.Reading(False, ("U1",))

        outcome = await audiences.refresh(
            MagicMock(), uuid4(), "slack", "C1", trigger="test", reader=reader
        )
    assert outcome.error is None
    record.assert_awaited_once()
    rewrite.assert_awaited_once()
    assert resolved.call_args.args[2] == "slack"
    assert apply.call_args.kwargs["unmatched"] == ["U9"]


@pytest.mark.asyncio
async def test_refresh_passes_none_when_the_read_fails() -> None:
    apply = AsyncMock(return_value=Applied(EMPTY, EMPTY, kept=True))

    async def reader() -> audiences.Reading:
        raise audiences.ReadFailed("down")

    with patch.object(stored, "apply", apply):
        outcome = await audiences.refresh(
            MagicMock(), uuid4(), "github", "o/r", trigger="test", reader=reader
        )
    assert apply.call_args.args[4] is None
    assert apply.call_args.kwargs["error"] == "ReadFailed: down"
    assert outcome.applied.kept


@pytest.mark.asyncio
async def test_record_params_hold_counts_only() -> None:
    a, b = str(uuid4()), str(uuid4())
    applied = Applied(Audience(False, frozenset({a})), Audience(False, frozenset({a, b})))
    conn = AsyncMock()
    engine = MagicMock()
    engine.begin.return_value.__aenter__.return_value = conn
    with patch.object(audiences.audit, "record", AsyncMock()) as record:
        await audiences._record(engine, uuid4(), "slack", "C1", "test", applied, None)
    params = record.call_args.kwargs["params"]
    assert params["members_added"] == 1 and params["members_removed"] == 0
    assert a not in str(params) and b not in str(params)


def test_audiences_commands_parse() -> None:
    import checkcord.cli.commands as cli

    parser = cli.build_parser()
    assert parser.parse_args(["audiences", "list"]).handler is cli.cmd_audiences_list
    args = parser.parse_args(["audiences", "refresh", "--platform", "github", "--container", "o/r"])
    assert args.handler is cli.cmd_audiences_refresh
    with pytest.raises(SystemExit):
        parser.parse_args(["audiences", "refresh", "--platform", "jira", "--container", "x"])


def test_audiences_refresh_needs_a_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    import checkcord.cli.commands as cli

    monkeypatch.setattr(cli, "_tenant", lambda args: uuid4())
    with pytest.raises(cli.CLIError, match="--all"):
        cli.cmd_audiences_refresh(
            argparse.Namespace(tenant=None, platform="slack", container=None, all=False)
        )


@pytest.mark.asyncio
async def test_doctor_warns_on_stale_audiences(monkeypatch: pytest.MonkeyPatch) -> None:
    from checkcord.services import doctor

    now = datetime.now(UTC)
    rows = [{"refreshed_at": now}, {"refreshed_at": now - 30 * HOUR}, {"refreshed_at": None}]
    conn = AsyncMock()
    engine = MagicMock()
    engine.begin.return_value.__aenter__.return_value = conn
    monkeypatch.setattr(doctor, "list_tenant_ids", AsyncMock(return_value=[uuid4()]))
    monkeypatch.setattr(stored, "list_rows", AsyncMock(return_value=rows))
    check = await doctor.audience_check(engine, 24)
    assert check.status == "WARN" and "2 of 3" in check.detail
    monkeypatch.setattr(stored, "list_rows", AsyncMock(return_value=rows[:1]))
    assert (await doctor.audience_check(engine, 24)).status == "PASS"


@pytest.mark.asyncio
async def test_slack_member_left_refreshes_the_stored_channel() -> None:
    from checkcord.adapters.chat.providers.slack.listeners import register_listeners

    class App:
        def __init__(self) -> None:
            self.listeners: dict[str, Any] = {}

        def event(self, name: str) -> Any:
            def wrap(fn: Any) -> Any:
                self.listeners[name] = fn
                return fn

            return wrap

    tenant = uuid4()
    conn = AsyncMock()
    result = MagicMock()
    result.first.return_value = (tenant, "xoxb-" + "x" * 8)
    conn.execute.return_value = result
    engine = MagicMock()
    engine.begin.return_value.__aenter__.return_value = conn
    bus = MagicMock()
    app = App()
    register_listeners(app=app, bus=bus, db_engine=engine)
    with patch.object(audiences, "refresh_stored", AsyncMock()) as refresh:
        await app.listeners["member_left_channel"](
            {"team_id": "T1"}, {"type": "member_left_channel", "channel": "G1", "user": "U1"}
        )
    args, kwargs = refresh.call_args
    assert args[1:] == (tenant, "slack", "G1")
    assert kwargs["trigger"] == "slack:member_left_channel"
    assert kwargs["details"] == {"team_id": "T1"} and kwargs["redis"] is bus.redis
