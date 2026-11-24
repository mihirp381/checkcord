"""Checks a tree is ready to tag, and prints the release notes for a version.

uv run python scripts/check_release.py                 # markers only
uv run python scripts/check_release.py --tag v0.1.0    # and the version, date and notes
uv run python scripts/check_release.py --tag v0.1.0 --notes-out notes.md
"""

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = "[" + "SET BEFORE PUBLISHING"
URL_PLACEHOLDER = "OWNER/REPO"
SKIP_DIRS = {".git", ".venv", "node_modules", ".next", "__pycache__"}


def tracked(root: Path) -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            check=True,
        ).stdout
        return [root / p for p in out.decode().split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        return [
            p
            for p in root.rglob("*")
            if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(root).parts)
        ]


def markers(root: Path) -> list[str]:
    found = []
    for path in tracked(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if MARKER in line:
                found.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
    return found


def notes(changelog: str, version: str) -> tuple[str | None, str]:
    """The heading's date (None if missing) and the section body for ``version``."""
    heading = re.compile(rf"^## \[{re.escape(version)}\](?: - (\S+))?\s*$", re.M)
    match = heading.search(changelog)
    if not match:
        raise ValueError(f"CHANGELOG.md has no '## [{version}]' section")
    rest = changelog[match.end() :]
    end = re.search(r"^## \[", rest, re.M)
    body = rest[: end.start() if end else None].strip()
    date = match.group(1)
    return (date if date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else None), body


def problems(root: Path, tag: str | None) -> tuple[list[str], str]:
    found = [f"unset placeholder: {m}" for m in markers(root)]
    if URL_PLACEHOLDER in (root / "pyproject.toml").read_text():
        found.append(
            f"unset placeholder: pyproject.toml: [project.urls] still say {URL_PLACEHOLDER}"
        )
    body = ""
    if tag is None:
        return found, body
    version = tag.removeprefix("v")
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    web = json.loads((root / "web" / "package.json").read_text())["version"]
    for name, value in (("pyproject.toml", pyproject), ("web/package.json", web)):
        if value != version:
            found.append(f"{name} says {value}, the tag says {version}")
    try:
        date, body = notes((root / "CHANGELOG.md").read_text(), version)
        if date is None:
            found.append(f"CHANGELOG.md: date the [{version}] heading as '- YYYY-MM-DD'")
        if not body:
            found.append(f"CHANGELOG.md: the [{version}] section is empty")
    except ValueError as err:
        found.append(str(err))
    return found, body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the tree is ready to release.")
    parser.add_argument("--tag", help="the tag being released, such as v0.1.0")
    parser.add_argument("--notes-out", type=Path, help="write the release notes here")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    found, body = problems(args.root, args.tag)
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        return 1
    if args.notes_out:
        args.notes_out.write_text(body + "\n")
    print("ready" + (f" to release {args.tag}" if args.tag else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
