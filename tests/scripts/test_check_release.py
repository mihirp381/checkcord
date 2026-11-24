import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts import check_release

MARK = "[" + "SET BEFORE PUBLISHING: contact]"


def _tree(
    tmp_path: Path, *, version: str = "1.2.0", heading: str = "## [1.2.0] - 2026-01-05"
) -> Path:
    (tmp_path / "web").mkdir()
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n')
    (tmp_path / "web" / "package.json").write_text(json.dumps({"version": version}))
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n{heading}\n\n### Added\n\n- A thing.\n\n"
        "## [1.1.0] - 2025-01-01\n\n- Old.\n"
    )
    return tmp_path


def test_a_ready_tree_passes_and_writes_only_its_own_notes(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    out = tmp_path / "notes.md"
    assert (
        check_release.main(["--root", str(root), "--tag", "v1.2.0", "--notes-out", str(out)]) == 0
    )
    assert out.read_text() == "### Added\n\n- A thing.\n"


def test_a_placeholder_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path)
    (root / "CODE_OF_CONDUCT.md").write_text(f"Report to {MARK}.\n")
    assert check_release.main(["--root", str(root)]) == 1
    assert "CODE_OF_CONDUCT.md:1" in capsys.readouterr().err


def test_placeholder_project_urls_fail(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    with (root / "pyproject.toml").open("a") as f:
        f.write('[project.urls]\nHomepage = "https://github.com/OWNER/REPO"\n')
    found, _ = check_release.problems(root, None)
    assert found == ["unset placeholder: pyproject.toml: [project.urls] still say OWNER/REPO"]


@pytest.mark.parametrize(
    ("version", "heading", "problem"),
    [
        ("1.1.9", "## [1.2.0] - 2026-01-05", "pyproject.toml says 1.1.9"),
        ("1.2.0", "## [1.2.0] - Unreleased", "date the [1.2.0] heading"),
        ("1.2.0", "## [1.3.0] - 2026-01-05", "no '## [1.2.0]' section"),
    ],
)
def test_version_date_and_section_must_match_the_tag(
    tmp_path: Path, version: str, heading: str, problem: str
) -> None:
    found, _ = check_release.problems(_tree(tmp_path, version=version, heading=heading), "v1.2.0")
    assert any(problem in p for p in found), found


def test_the_repository_has_a_section_for_its_version() -> None:
    root = Path(check_release.ROOT)
    found, _ = check_release.problems(root, None)
    assert all(
        p.startswith(
            ("unset placeholder: CODE_OF_CONDUCT.md:", "unset placeholder: pyproject.toml:")
        )
        for p in found
    ), found
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    _, body = check_release.notes((root / "CHANGELOG.md").read_text(), version)
    assert body


def test_runs_as_a_script(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    done = subprocess.run(
        [sys.executable, str(Path(check_release.__file__)), "--root", str(root)],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0 and "ready" in done.stdout
