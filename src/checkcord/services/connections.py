from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.core.config import Settings, get_settings
from checkcord.storage import audit
from checkcord.storage.models import ConnectorInstallation, JiraProjectKey
from checkcord.storage.postgres import events_table, tenant_context

logger = structlog.get_logger(__name__).bind(component="connections")


def tables() -> list[tuple[str, Any, Any]]:
    """(provider, model, identifier column) of every connector that stores its connections."""
    from checkcord import connectors

    return [
        (c.name, c.table, getattr(c.table, c.identifier))
        for c in connectors.connectors()
        if c.table is not None and c.identifier is not None
    ]


def providers() -> list[str]:
    return [provider for provider, _, _ in tables()]


def _of(model: Any, provider: str) -> Any:
    """Rows of one provider; the shared table holds several."""
    return model.provider == provider if model is ConnectorInstallation else True


SCOPE_DAYS = 7

# Events sent to the push API, from any key of the tenant.
PUSH_CONNECTION = "custom:push"


def connection_key(provider: str, identifier: object) -> str:
    """The ``events.connection`` value of events ingested through one connection."""
    return f"{provider}:{str(identifier).strip()}"


async def disconnect(
    engine: AsyncEngine,
    tenant_id: UUID,
    provider: str,
    identifier: str,
    settings: Settings | None = None,
    actor: audit.Actor = audit.CLI_ACTOR,
) -> bool:
    """Delete one of the tenant's connections, after the connector's own clean-up at the
    provider."""
    from checkcord import connectors

    found = {p: (model, column) for p, model, column in tables()}
    if provider not in found:
        raise ValueError(f"unknown provider {provider!r}")
    model, column = found[provider]
    key: Any = identifier.strip()
    if column is model.id:
        try:
            key = UUID(key)
        except ValueError:
            return False

    async with tenant_context(engine, tenant_id) as conn:
        row = (
            (
                await conn.execute(
                    select(model).where(
                        column == key, model.tenant_id == tenant_id, _of(model, provider)
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        return False

    connector = connectors.get(provider)
    if connector is not None and connector.disconnect is not None:
        await connector.disconnect(row, settings or get_settings())

    async with tenant_context(engine, tenant_id) as conn:
        await conn.execute(delete(model).where(model.id == row["id"]))
        await audit.record(
            conn,
            tenant_id,
            actor,
            "connection.removed",
            params={"provider": provider, "identifier": str(key)},
        )
    logger.info("connection_removed", provider=provider, tenant_id=str(tenant_id))
    return True


_EARLIEST = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Connection:
    provider: str
    identifier: str
    created_at: datetime | None
    in_review: bool = False


async def list_connections(engine: AsyncEngine, tenant_id: UUID) -> list[Connection]:
    from checkcord.services import review

    found: list[Connection] = []
    async with tenant_context(engine, tenant_id) as conn:
        state = await review.states(conn, tenant_id)
        for provider, model, identifier in tables():
            rows = await conn.execute(
                select(identifier, model.created_at).where(
                    model.tenant_id == tenant_id, _of(model, provider)
                )
            )
            for i, created in rows.fetchall():
                key = connection_key(provider, i)
                found.append(Connection(provider, str(i), created, state.pop(key, False)))
        project_keys = await conn.execute(
            select(JiraProjectKey.project_key, JiraProjectKey.created_at).where(
                JiraProjectKey.tenant_id == tenant_id
            )
        )
        found.extend(
            Connection("jira-project", key, created) for key, created in project_keys.fetchall()
        )
    # Sources with no stored installation, such as the push API, and connections put in
    # review before they were installed.
    stored = providers()
    for key, in_review in state.items():
        provider, _, identifier = key.partition(":")
        if in_review or provider not in stored:
            found.append(Connection(provider, identifier, None, in_review))
    return sorted(found, key=lambda c: (c.provider, c.created_at or _EARLIEST))


async def with_activity(engine: AsyncEngine, tenant_id: UUID) -> list[dict[str, Any]]:
    """Each connection with its state, scope, event count, held records and newest event."""
    from checkcord import connectors
    from checkcord.services import review, scope

    _ev = events_table.c
    async with tenant_context(engine, tenant_id, include_held=True) as conn:
        activity: dict[str, tuple[datetime, int]] = {
            key: (last, n)
            for key, last, n in (
                await conn.execute(
                    select(_ev.connection, func.max(_ev.ingested_at), func.count())
                    .where((_ev.tenant_id == tenant_id) & _ev.connection.is_not(None))
                    .group_by(_ev.connection)
                )
            ).all()
        }
        held = await review.held_counts(conn, tenant_id)
    rules = await scope.rules(engine, tenant_id)
    skipped = await scope.skipped(engine, tenant_id, SCOPE_DAYS)

    def scoped(key: str) -> dict[str, Any]:
        rule = rules.get(key, scope.Rule())
        return {
            "scope": (
                {"include": list(rule.include), "exclude": list(rule.exclude)}
                if scope.supported(key)
                else None
            ),
            "scope_skipped": skipped.get(key, 0),
        }

    found: list[dict[str, Any]] = []
    for c in await list_connections(engine, tenant_id):
        key = connection_key(c.provider, c.identifier)
        last, n = activity.pop(key, (None, 0))
        found.append(
            {
                "connection": key,
                "provider": c.provider,
                "identifier": c.identifier,
                "status": connectors.status(c.provider),
                "state": "review" if c.in_review else "live",
                "created_at": c.created_at,
                "last_event_at": last,
                "events": n,
                "held_records": held.get(key, 0),
                **scoped(key),
            }
        )
    for key, (last, n) in sorted(activity.items()):
        provider, _, identifier = key.partition(":")
        found.append(
            {
                "connection": key,
                "provider": provider,
                "identifier": identifier,
                "status": connectors.status(provider),
                "state": "live",
                "created_at": None,
                "last_event_at": last,
                "events": n,
                "held_records": held.get(key, 0),
                **scoped(key),
            }
        )
    return found


def oauth_apps() -> dict[str, tuple[str, str, str]]:
    """Connectors installed through an OAuth link: (title, client id field, redirect field)."""
    from checkcord import connectors

    return {
        c.name: (c.title, c.oauth.client_id_field, c.oauth.redirect_field)
        for c in connectors.connectors()
        if c.oauth is not None
    }


@dataclass(frozen=True)
class InstallLink:
    provider: str
    name: str
    url: str
    redirect_uri: str
    expires_in_seconds: int


def install_link(settings: Settings, provider: str, tenant_id: UUID, base_url: str) -> InstallLink:
    """A one-time install link; ValueError when the provider's OAuth app is not configured."""
    from checkcord.core.oauth_state import STATE_TTL_SECONDS, install_url

    name, client_id_field, redirect_field = oauth_apps()[provider]
    redirect_uri = getattr(settings, redirect_field)
    if not getattr(settings, client_id_field) or not redirect_uri:
        raise ValueError(
            f"{name} OAuth is not configured: set {client_id_field.upper()}, "
            f"its client secret and {redirect_field.upper()}"
        )
    return InstallLink(
        provider,
        name,
        install_url(base_url, provider, tenant_id),
        redirect_uri,
        STATE_TTL_SECONDS,
    )
