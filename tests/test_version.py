import json
import subprocess
import sys
import tomllib
from pathlib import Path

from checkcord.core.version import checkcord_version
from checkcord.main import app

ROOT = Path(__file__).resolve().parents[1]


def test_every_place_shows_the_pyproject_version() -> None:
    wanted = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert checkcord_version() == wanted
    assert app.openapi()["info"]["version"] == wanted
    assert json.loads((ROOT / "web" / "package.json").read_text())["version"] == wanted
    lock = json.loads((ROOT / "web" / "package-lock.json").read_text())
    assert lock["version"] == lock["packages"][""]["version"] == wanted


def test_cli_prints_the_version() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "checkcord.cli", "--version"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip() == f"checkcord {checkcord_version()}"
