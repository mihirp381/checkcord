"""Connector definitions: what the CLI, admin API, scope, audiences, scheduler and doctor read."""

from checkcord.connectors.base import (
    STATUSES,
    Connector,
    Listener,
    ListenerContext,
    OAuthApp,
    Poll,
    Setup,
    SetupContext,
    Status,
)
from checkcord.connectors.registry import ENTRY_POINT_GROUP, connectors, get, load_errors, status

__all__ = [
    "ENTRY_POINT_GROUP",
    "STATUSES",
    "Connector",
    "Listener",
    "ListenerContext",
    "OAuthApp",
    "Poll",
    "Setup",
    "SetupContext",
    "Status",
    "connectors",
    "get",
    "load_errors",
    "status",
]
