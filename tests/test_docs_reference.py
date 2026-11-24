import argparse
import itertools
import os
import re
import shlex
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from checkcord.cli.commands import build_parser
from checkcord.core.config import Settings
from checkcord.proxy.config import ProxySettings

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", *sorted((ROOT / "docs").glob("*.md"))]

PREFIX = re.compile(
    r"^(?:\$ )?(?:docker compose (?:exec|run --rm) \w+ |uv run |kubectl exec \S+ -- )?"
)
PLACEHOLDER = re.compile(r"<[^>]+>|[A-Z][A-Z_]*")
CANDIDATES = ("x", "00000000-0000-0000-0000-000000000001", "slack", "github", "2026-01-01", "7")
NOT_SETTINGS = {"RECORD_ID", "EVENT_ID", "TEAM_ID", "TENANT_ID", "CONNECTION_ID", "USER_ID"}
OTHER_PROGRAMS = {"OLLAMA_HOST", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"}


def _code(text: str) -> Iterator[str]:
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            yield line.strip()
    yield from re.findall(r"(?<!`)`([^`\n]+)`(?!`)", text)


def commands() -> list[tuple[str, str]]:
    found = []
    for doc in DOCS:
        for snippet in _code(doc.read_text()):
            line = PREFIX.sub("", snippet.split(" #")[0].strip())
            if re.match(r"checkcord( |$)", line) and not line.startswith("checkcord-"):
                found.append((doc.name, line))
    return found


class Refused(Exception):
    pass


def _strict(parser: argparse.ArgumentParser) -> None:
    def error(message: str) -> None:
        raise Refused(message)

    parser.error = error  # type: ignore[method-assign]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                _strict(sub)


def _tokens(line: str) -> list[str]:
    line = re.sub(r"<[^>]+>", lambda m: m.group().replace(" ", "_"), line)
    tokens = []
    for token in shlex.split(line.replace("…", "").replace("...", "")):
        if token in (">", "|"):
            break
        tokens.append(token.split("|")[0] if not token.startswith("-") else token)
    return tokens


def _attempts(tokens: list[str]) -> Iterator[list[str]]:
    holes = [i for i, t in enumerate(tokens) if PLACEHOLDER.fullmatch(t)]
    for values in itertools.product(CANDIDATES, repeat=len(holes)):
        filled = list(tokens)
        for i, value in zip(holes, values, strict=True):
            filled[i] = value
        yield filled


def _refusal(tokens: list[str]) -> str | None:
    """None if some filling of the placeholders parses, else the first refusal."""
    first = None
    for args in _attempts(tokens):
        parser = build_parser()
        _strict(parser)
        try:
            parser.parse_args(args)
            return None
        except Refused as err:
            if "is required" in str(err) or "arguments are required" in str(err):
                return None
            first = first or str(err)
        except SystemExit as exit_:
            if exit_.code == 0:
                return None
            first = first or f"exit {exit_.code}"
    return first


@pytest.mark.parametrize(("doc", "line"), commands())
def test_documented_command_parses(doc: str, line: str) -> None:
    tokens = _tokens(line)[1:]
    if tokens and PLACEHOLDER.fullmatch(tokens[0]):
        return
    refusal = _refusal(tokens)
    assert refusal is None, f"{doc}: `{line}`: {refusal}"


def known_names() -> set[str]:
    names = {n.upper() for n in Settings.model_fields}
    names |= {f"CHECKCORD_PROXY_{n.upper()}" for n in ProxySettings.model_fields}
    for path in [
        ROOT / "docker-compose.yml",
        *ROOT.glob(".env*.example"),
        *ROOT.glob("web/src/**/*.ts*"),
        *ROOT.glob("scripts/*.sh"),
    ]:
        text = path.read_text()
        names |= set(re.findall(r"\$\{([A-Z][A-Z0-9_]+)", text))
        names |= set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.M))
        names |= set(re.findall(r"^\s+([A-Z][A-Z0-9_]+):", text, re.M))
        names |= set(re.findall(r"process\.env\.([A-Z][A-Z0-9_]+)", text))
    return names


def named_settings() -> list[tuple[str, str]]:
    found = set()
    for doc in DOCS:
        for snippet in _code(doc.read_text()):
            for name in re.findall(
                r"(?<![\w$/.-])([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)(?==|\b)(?!\.\w)", snippet
            ):
                if name not in NOT_SETTINGS | OTHER_PROGRAMS:
                    found.add((doc.name, name))
    return sorted(found)


def test_every_documented_setting_exists() -> None:
    known = known_names()
    missing = [f"{doc}: {name}" for doc, name in named_settings() if name not in known]
    assert missing == []


def test_the_checks_find_something() -> None:
    assert len(commands()) > 50
    assert len(named_settings()) > 50


def _shape(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path.rstrip("/"))


def documented_routes() -> list[tuple[str, str, str]]:
    found = set()
    for doc in DOCS:
        text = doc.read_text()
        for method, path in re.findall(
            r"\b(GET|POST|PUT|PATCH|DELETE)(?:\|[A-Z|]+)? (/[\w/{}.-]*)", text
        ):
            found.add((doc.name, method, path.rstrip(".")))
    return sorted(found)


def _matches(documented: str, served: str) -> bool:
    """A served ``{}`` segment also matches a literal value, such as ``/accept``."""
    ours, theirs = documented.split("/"), served.split("/")
    return len(ours) == len(theirs) and all(
        a == b or b == "{}" for a, b in zip(ours, theirs, strict=False)
    )


def _served(routes: list[Any], prefix: str = "") -> Iterator[tuple[str, str]]:
    for route in routes:
        if hasattr(route, "original_router"):
            inner = prefix + (route.include_context.prefix or "")
            yield from _served(route.original_router.routes, inner)
        for method in getattr(route, "methods", None) or ():
            yield method, _shape(prefix + route.path)


def test_every_documented_route_exists() -> None:
    from checkcord.main import app

    served = set(_served(app.routes)) | {("GET", "")}
    relative = {
        (m, p.removeprefix(base))
        for m, p in served
        for base in ("/scim/v2", "/api/v1/admin")
        if p.startswith(base)
    }
    missing = [
        f"{doc}: {method} {path}"
        for doc, method, path in documented_routes()
        if not any(m == method and _matches(_shape(path), p) for m, p in served | relative)
    ]
    assert missing == []


REPO_PATH = re.compile(
    r"(?:\.github|alembic|deploy|docs|examples|scripts|src|tests|web)/[\w./{}-]*[\w}]"
    r"|[\w.-]+\.(?:md|yml|yaml|toml|sql|sh|example)(?![\w.])"
)


GENERATED = {"ARCHITECTURE.md", "summary.md"}


def _file_names() -> set[str]:
    names = set()
    for _top, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "node_modules", ".next"}]
        names.update(files)
    return names


def test_every_documented_file_exists() -> None:
    names = _file_names()
    missing = []
    for doc in DOCS:
        text = doc.read_text()
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
            if not re.match(r"[a-z]+:", target) and not (doc.parent / target).exists():
                missing.append(f"{doc.name}: link {target}")
        for snippet in re.findall(r"`([^`\n]+)`", text):
            for path in REPO_PATH.findall(snippet):
                if "{" in path or "*" in path or "<" in path or path in GENERATED:
                    continue
                if "/" not in path and path in names:
                    continue
                if not any((base / path).exists() for base in (ROOT, doc.parent, ROOT / "web")):
                    missing.append(f"{doc.name}: {path}")
    assert sorted(set(missing)) == []
