"""SCIM 2.0 wire details shared by the Users, Groups and discovery endpoints (RFC 7643, 7644)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from checkcord.core.config import get_settings

USER = "urn:ietf:params:scim:schemas:core:2.0:User"
ENTERPRISE = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_OP = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
MEDIA_TYPE = "application/scim+json"
MAX_RESULTS = 200
READ_ONLY = {"id", "meta", "schemas", "groups", "password"}


class SCIMException(Exception):
    def __init__(self, status_code: int, detail: str, scim_type: str | None = None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.scim_type = scim_type

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"schemas": [ERROR], "status": str(self.status_code)}
        if self.scim_type:
            out["scimType"] = self.scim_type
        out["detail"] = self.detail
        return out


class SCIMResponse(JSONResponse):
    media_type = MEDIA_TYPE


def not_found(kind: str) -> SCIMException:
    return SCIMException(404, f"{kind} not found")


def base_url(request: Request) -> str:
    root = get_settings().public_base_url or str(request.base_url)
    return root.rstrip("/") + "/scim/v2"


async def read_body(request: Request) -> dict[str, Any]:
    try:
        data = json.loads(await request.body() or b"null")
    except ValueError as err:
        raise SCIMException(400, "The request body is not JSON", "invalidSyntax") from err
    if not isinstance(data, dict):
        raise SCIMException(400, "The request body must be a JSON object", "invalidSyntax")
    return data


def params(request: Request) -> dict[str, str]:
    return {k.lower(): v for k, v in request.query_params.items()}


def get_ci(data: dict[str, Any], name: str, default: Any = None) -> Any:
    for key, value in data.items():
        if key.lower() == name.lower():
            return value
    return default


def canon(data: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """Rename keys to their canonical case; attribute names are case-insensitive."""
    return {names.get(k.lower(), k): v for k, v in data.items()}


def strip_urn(name: str, urn: str) -> str:
    return name[len(urn) + 1 :] if name.lower().startswith(urn.lower() + ":") else name


def as_bool(value: Any, name: str = "active") -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise SCIMException(400, f"{name} must be true or false", "invalidValue")


@dataclass(frozen=True)
class Page:
    start: int
    count: int


def _int(raw: str | None, default: int, name: str) -> int:
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as err:
        raise SCIMException(400, f"{name} must be an integer", "invalidValue") from err


def page(query: dict[str, str]) -> Page:
    start = _int(query.get("startindex"), 1, "startIndex")
    count = _int(query.get("count"), MAX_RESULTS, "count")
    return Page(max(start, 1), min(max(count, 0), MAX_RESULTS))


def list_response(resources: list[dict[str, Any]], total: int, at: Page) -> dict[str, Any]:
    return {
        "schemas": [LIST],
        "totalResults": total,
        "startIndex": at.start,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


_STRING = r'"((?:[^"\\]|\\.)*)"'
_FILTER = re.compile(rf"^\s*([\w.:$-]+)\s+eq\s+{_STRING}\s*$", re.IGNORECASE)
_PATH = re.compile(
    rf"^([\w$-]+)(?:\[\s*([\w.$-]+)\s+eq\s+(?:{_STRING}|(true|false))\s*\])?(?:\.([\w$-]+))?$",
    re.IGNORECASE,
)


def _unquote(raw: str) -> str:
    return str(json.loads(f'"{raw}"'))


def parse_filter(text: str | None, allowed: dict[str, str], urn: str) -> tuple[str, str] | None:
    """Only `attribute eq "value"` is supported, which is what Okta and Entra send."""
    if not text:
        return None
    found = _FILTER.match(text)
    if not found:
        raise SCIMException(
            400, 'Only filters of the form attribute eq "value" work', "invalidFilter"
        )
    name = strip_urn(found.group(1), urn).lower()
    if name not in allowed:
        raise SCIMException(
            400,
            f"Cannot filter on {found.group(1)}; use {' or '.join(allowed.values())}",
            "invalidFilter",
        )
    return allowed[name], _unquote(found.group(2))


@dataclass(frozen=True)
class Path:
    attr: str
    where: tuple[str, str] | None = None
    sub: str | None = None


def parse_path(path: str) -> Path:
    found = _PATH.match(path.strip())
    if not found:
        raise SCIMException(400, f"Cannot read the path {path}", "invalidPath")
    attr, where_attr, where_value, where_bool, sub = found.groups()
    where = None
    if where_attr:
        where = (where_attr, _unquote(where_value) if where_value is not None else where_bool)
    return Path(attr, where, sub)


@dataclass(frozen=True)
class Operation:
    op: str
    path: str | None
    value: Any


def operations(body: dict[str, Any]) -> list[Operation]:
    ops = get_ci(body, "Operations")
    if not isinstance(ops, list) or not ops:
        raise SCIMException(400, "A PATCH needs a non-empty Operations list", "invalidSyntax")
    out = []
    for raw in ops:
        if not isinstance(raw, dict):
            raise SCIMException(400, "Each operation must be an object", "invalidSyntax")
        op = str(get_ci(raw, "op", "")).lower()
        if op not in ("add", "replace", "remove"):
            raise SCIMException(400, f"Unknown op {op!r}", "invalidSyntax")
        path = get_ci(raw, "path")
        if path is not None and not isinstance(path, str):
            raise SCIMException(400, "path must be a string", "invalidPath")
        out.append(Operation(op, path or None, get_ci(raw, "value")))
    return out


def _key(container: dict[str, Any], name: str, names: Callable[[str], str]) -> str:
    for key in container:
        if key.lower() == name.lower():
            return key
    return names(name)


def apply(
    resource: dict[str, Any],
    op: str,
    path: str | None,
    value: Any,
    names: Callable[[str], str],
    extensions: tuple[str, ...] = (),
    core: str = USER,
) -> None:
    """Apply one PATCH operation to a resource held as a plain dict."""
    if path is None:
        if op == "remove":
            raise SCIMException(400, "remove needs a path", "noTarget")
        if not isinstance(value, dict):
            raise SCIMException(400, "An operation without a path needs an object", "invalidValue")
        for key, item in value.items():
            urn = next((u for u in extensions if u.lower() == key.lower()), None)
            if urn and isinstance(item, dict):
                for sub_key, sub_item in item.items():
                    apply(resource, op, f"{urn}:{sub_key}", sub_item, names, extensions, core)
            else:
                apply(resource, op, key, item, names, extensions, core)
        return

    container = resource
    path = strip_urn(path, core)
    for urn in extensions:
        if path.lower().startswith(urn.lower() + ":"):
            container = resource.setdefault(urn, {})
            path = path[len(urn) + 1 :]
            break
    target = parse_path(path)
    key = _key(container, target.attr, names)

    if target.where is None and target.sub is None:
        if op == "remove":
            container.pop(key, None)
            return
        current = container.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            current.update(value)
        elif op == "add" and isinstance(current, list) and isinstance(value, list):
            container[key] = current + [v for v in value if v not in current]
        else:
            container[key] = value
        return

    if target.where is None:
        assert target.sub is not None
        current = container.setdefault(key, {})
        items = current if isinstance(current, list) else [current]
        for item in items:
            if isinstance(item, dict):
                sub = _key(item, target.sub, lambda s: s)
                if op == "remove":
                    item.pop(sub, None)
                else:
                    item[sub] = value
        return

    where_attr, where_value = target.where
    current = container.get(key)
    items = list(current) if isinstance(current, list) else []

    def matches(item: Any) -> bool:
        return (
            isinstance(item, dict)
            and str(get_ci(item, where_attr, "")).lower() == where_value.lower()
        )

    chosen = [item for item in items if matches(item)]
    if op == "remove":
        if target.sub:
            for item in chosen:
                item.pop(_key(item, target.sub, lambda s: s), None)
        else:
            items = [item for item in items if not matches(item)]
        container[key] = items
        return
    if not chosen:
        fresh: dict[str, Any] = {
            where_attr: where_value == "true" if where_value in ("true", "false") else where_value
        }
        items.append(fresh)
        chosen = [fresh]
    for item in chosen:
        if target.sub:
            item[_key(item, target.sub, lambda s: s)] = value
        elif isinstance(value, dict):
            item.update(value)
    container[key] = items
