import importlib
from importlib.metadata import entry_points

from checkcord.connectors.base import Connector

ENTRY_POINT_GROUP = "checkcord.connectors"

_BUILTIN_MODULES = (
    "checkcord.adapters.code.providers.github.connector",
    "checkcord.adapters.tasks.providers.jira.connector",
    "checkcord.adapters.monitoring.providers.sentry.connector",
    "checkcord.adapters.meetings.providers.zoom.connector",
    "checkcord.adapters.meetings.providers.msteams.connector",
    "checkcord.adapters.chat.providers.slack.connector",
    "checkcord.adapters.chat.providers.msteams.connector",
    "checkcord.adapters.tasks.providers.notion.connector",
    "checkcord.api.push_connector",
    "checkcord.adapters.tasks.providers.linear.connector",
    "checkcord.adapters.monitoring.providers.datadog.connector",
    "checkcord.connectors.signed_webhook",
)

_loaded: dict[str, Connector] | None = None
_errors: list[str] = []


def _as_list(value: object) -> list[Connector]:
    items: list[object] = list(value) if isinstance(value, list | tuple) else [value]
    found = [item for item in items if isinstance(item, Connector)]
    if len(found) != len(items):
        raise TypeError("an entry point must name a Connector or a list of them")
    return found


def _load() -> dict[str, Connector]:
    global _loaded
    if _loaded is not None:
        return _loaded
    found: dict[str, Connector] = {}
    for module in _BUILTIN_MODULES:
        for connector in importlib.import_module(module).CONNECTORS:
            found[connector.name] = connector
    errors: list[str] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            loaded = _as_list(ep.load())
            taken = [c.name for c in loaded if c.name in found]
            if taken:
                raise ValueError(f"{', '.join(taken)} already registered")
        except Exception as err:
            errors.append(f"{ep.name} ({ep.value}): {type(err).__name__}: {err}")
            continue
        found.update((c.name, c) for c in loaded)
    _loaded = found
    _errors[:] = errors
    return found


def connectors() -> list[Connector]:
    return list(_load().values())


def get(name: str) -> Connector | None:
    return _load().get(name)


def load_errors() -> list[str]:
    _load()
    return list(_errors)


def status(provider: str) -> str | None:
    connector = get(provider)
    return connector.status if connector else None


def reload() -> None:
    global _loaded
    _loaded = None
