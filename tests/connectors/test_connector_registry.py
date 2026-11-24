import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from checkcord import connectors
from checkcord.cli import commands as cli
from checkcord.connectors import registry
from checkcord.services import audiences, connections, doctor, scope

PLUGIN = Path(__file__).resolve().parents[1] / "fixtures" / "connector_plugin"


def _install(tmp_path: Path, entry: str) -> None:
    dist = tmp_path / "checkcord_example_plugin-0.1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: checkcord-example-plugin\n")
    (dist / "entry_points.txt").write_text(f"[checkcord.connectors]\nexample = {entry}\n")


@pytest.fixture
def plugin_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.syspath_prepend(str(PLUGIN))
    monkeypatch.syspath_prepend(str(tmp_path))
    registry.reload()
    yield tmp_path
    sys.modules.pop("checkcord_example_plugin", None)
    registry.reload()


def test_names_are_unique_and_every_table_has_its_identifier() -> None:
    found = connectors.connectors()
    assert len({c.name for c in found}) == len(found)
    for c in found:
        if c.table is not None:
            assert hasattr(c.table, c.identifier or ""), c.name
            assert all(hasattr(c.table, col) for col in c.secret_columns), c.name


def test_everything_that_lists_connectors_reads_the_registry() -> None:
    found = {c.name: c for c in connectors.connectors()}
    assert set(connections.providers()) == {n for n, c in found.items() if c.table is not None}
    assert set(scope.providers()) == {n for n, c in found.items() if c.scope}
    assert set(audiences.platforms()) == {"slack", "ms_teams", "github"}
    assert set(connections.oauth_apps()) == {"slack", "teams", "notion", "linear"}


def test_connect_help_shows_each_status(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["connect", "--help"])
    out = capsys.readouterr().out
    assert "[beta]" in out and "[experimental]" in out
    assert "webhook" in out and "datadog" in out


def test_connections_list_shows_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [
        connections.Connection("slack", "T1", None),
        connections.Connection("jira-project", "PAY", None),
    ]
    monkeypatch.setattr(cli, "_with_engine", lambda fn: (rows, []))
    cli.main(["connections", "--tenant", str(uuid4())])
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["PROVIDER", "STATUS", "CREATED", "STATE", "IDENTIFIER", "AUDIENCE"]
    assert out[1].split()[:2] == ["slack", "beta"]
    assert out[1].split()[-2:] == ["from", "source"]
    assert out[2].split()[:2] == ["jira-project", "-"]


def test_bad_definitions_are_refused() -> None:
    with pytest.raises(ValueError, match="status"):
        connectors.Connector("x", "X", "webhook", "stable")
    with pytest.raises(ValueError, match="not a connector name"):
        connectors.Connector("a:b", "X", "webhook", "beta")
    with pytest.raises(ValueError, match="together"):
        connectors.Connector("x", "X", "webhook", "beta", identifier="id")


def test_an_installed_package_registers_a_connector(plugin_path: Path) -> None:
    _install(plugin_path, "checkcord_example_plugin:CONNECTORS")
    registry.reload()

    example = connectors.get("example")
    assert example is not None and connectors.load_errors() == []
    assert scope.providers()["example"] == "example folders"
    assert "example" not in connections.providers()
    assert cli._labelled("example", "x") == "x [experimental]"
    assert doctor.connectors_check().ok


def test_a_broken_package_is_reported_and_skipped(plugin_path: Path) -> None:
    _install(plugin_path, "checkcord_example_plugin:router")
    registry.reload()

    assert connectors.get("example") is None
    assert connectors.get("datadog") is not None
    check = doctor.connectors_check()
    assert not check.ok and "example" in check.detail


def test_a_package_cannot_replace_a_built_in(plugin_path: Path) -> None:
    import checkcord_example_plugin as plugin

    plugin.TAKEN = [connectors.Connector("slack", "Fake", "webhook", "beta")]
    _install(plugin_path, "checkcord_example_plugin:TAKEN")
    registry.reload()

    slack = connectors.get("slack")
    assert slack is not None and slack.title == "Slack"
    assert "already registered" in " ".join(connectors.load_errors())
