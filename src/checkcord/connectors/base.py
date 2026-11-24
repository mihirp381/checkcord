from __future__ import annotations

import argparse
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

if TYPE_CHECKING:
    from fastapi import APIRouter
    from sqlalchemy.ext.asyncio import AsyncEngine

    from checkcord.bus.interface import EventBus
    from checkcord.core.config import Settings

Status = Literal["beta", "experimental"]
# Where an event's audience comes from: the source's own members, the event itself, or an
# admin's setting for the connection.
AudienceFrom = Literal["source", "event", "admin"]

STATUSES: dict[str, str] = {
    "beta": "reads each event's audience from the source, keeps it current, honours the "
    "scope, and has unit and integration tests",
    "experimental": "misses one or more of the beta criteria",
}


@dataclass(frozen=True)
class SetupContext:
    """What `checkcord connect NAME` hands a connector's setup."""

    args: argparse.Namespace
    engine: AsyncEngine
    tenant_id: UUID
    public_url: str
    review: bool


@dataclass(frozen=True)
class Setup:
    """`checkcord connect NAME`: extra arguments, then a run that prints the setup steps.

    The CLI adds --tenant, --public-url and --review itself. Raise ValueError for a
    message the operator should see.
    """

    help: str
    run: Callable[[SetupContext], Awaitable[None]]
    arguments: Callable[[argparse.ArgumentParser], None] | None = None


@dataclass(frozen=True)
class Poll:
    """A scheduled job; it runs in one process per interval and returns events published.

    ``run`` gets the engine and an ``EventBus``. A job that ``publishes`` is scheduled only
    when Redis is configured; one that does not gets ``None`` for the bus.
    """

    job_id: str
    run: Callable[[AsyncEngine, Any], Awaitable[int | None]]
    minutes: Callable[[Settings], float]
    publishes: bool = True


@dataclass(frozen=True)
class ListenerContext:
    settings: Settings
    engine: AsyncEngine
    bus: EventBus


@dataclass(frozen=True)
class Listener:
    """A task that runs as long as its process: started after the database and the bus,
    cancelled before they close."""

    run: Callable[[ListenerContext], Awaitable[None]]
    process: Literal["api", "worker"]


@dataclass(frozen=True)
class OAuthApp:
    """Settings fields an install link needs, and lines `checkcord connect NAME` adds."""

    client_id_field: str
    redirect_field: str
    notes: Callable[[Settings], list[str]] | None = None


@dataclass(frozen=True)
class Connector:
    """One source Checkcord ingests from. ``name`` is the PROVIDER part of its connection keys."""

    name: str
    title: str
    ingestion: str
    status: Status
    scope: str | None = None
    audience_platform: str | None = None
    audience_from: AudienceFrom = "event"
    table: Any = None
    identifier: str | None = None
    secret_columns: tuple[str, ...] = ()
    sealed_json: tuple[tuple[str, Callable[[dict[str, Any]], dict[str, Any]]], ...] = ()
    routers: tuple[tuple[APIRouter, str | None], ...] = ()
    poll: Poll | None = None
    listener: Listener | None = None
    setup: Setup | None = None
    oauth: OAuthApp | None = None
    disconnect: Callable[[Mapping[str, Any], Settings], Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"{self.name}: status must be one of {', '.join(STATUSES)}")
        if ":" in self.name or not self.name:
            raise ValueError(f"{self.name!r} is not a connector name")
        if self.audience_from not in ("source", "event", "admin"):
            raise ValueError(f"{self.name}: audience_from must be source, event or admin")
        if (self.table is None) != (self.identifier is None):
            raise ValueError(f"{self.name}: table and identifier go together")
