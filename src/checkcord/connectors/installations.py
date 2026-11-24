"""Stored connections for connectors without a table of their own (`connector_installations`)."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.core.crypto import decrypt_secret, encrypt_secret
from checkcord.services import review as reviews
from checkcord.services.connections import connection_key
from checkcord.storage import audit
from checkcord.storage.models import ConnectorInstallation
from checkcord.storage.postgres import app_bypass_rls_context, tenant_context

_t = ConnectorInstallation.__table__.c


@dataclass(frozen=True)
class Installation:
    id: UUID
    tenant_id: UUID
    provider: str
    identifier: str
    secret: str | None
    config: dict[str, Any]


@dataclass(frozen=True)
class Saved:
    id: UUID
    created: bool


def _installation(row: Any) -> Installation:
    return Installation(
        row.id,
        row.tenant_id,
        row.provider,
        row.identifier,
        decrypt_secret(row.secret) if row.secret else None,
        dict(row.config or {}),
    )


async def save(
    engine: AsyncEngine,
    tenant_id: UUID,
    provider: str,
    identifier: str,
    *,
    secret: str | None,
    replace_secret: bool = True,
    config: dict[str, Any] | None = None,
    review: bool = False,
    actor: audit.Actor = audit.CLI_ACTOR,
) -> Saved:
    """Add or update one connection. An existing secret is kept unless ``replace_secret``."""
    identifier = identifier.strip()
    if not identifier:
        raise ValueError("the identifier must not be empty")
    sealed = encrypt_secret(secret) if secret else None
    async with tenant_context(engine, tenant_id) as conn:
        existing = await conn.scalar(
            select(_t.id)
            .where(_t.tenant_id == tenant_id, _t.provider == provider, _t.identifier == identifier)
            .with_for_update()
        )
        if existing is None:
            row_id = await conn.scalar(
                pg_insert(ConnectorInstallation)
                .values(
                    tenant_id=tenant_id,
                    provider=provider,
                    identifier=identifier,
                    secret=sealed,
                    config=config or {},
                )
                .returning(_t.id)
            )
        else:
            row_id = existing
            values: dict[str, Any] = {"updated_at": func.now()}
            if replace_secret:
                values["secret"] = sealed
            if config is not None:
                values["config"] = config
            await conn.execute(update(ConnectorInstallation).where(_t.id == row_id).values(values))
        await reviews.mark_new(conn, tenant_id, connection_key(provider, identifier), review)
        await audit.record(
            conn,
            tenant_id,
            actor,
            "connection.added" if existing is None else "connection.updated",
            params={"provider": provider, "identifier": identifier},
        )
    return Saved(row_id, existing is None)


async def by_id(engine: AsyncEngine, provider: str, installation_id: UUID) -> Installation | None:
    """Look a connection up from an inbound request, before its tenant is known."""
    async with app_bypass_rls_context(engine) as conn:
        row = (
            await conn.execute(
                select(ConnectorInstallation).where(
                    _t.id == installation_id, _t.provider == provider
                )
            )
        ).one_or_none()
    return _installation(row) if row else None
