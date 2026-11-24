"""Helpers for a connector's `checkcord connect` setup."""

import argparse
import getpass
import os
import secrets
import sys
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.connectors.base import SetupContext
from checkcord.core.crypto import encrypt_secret
from checkcord.services import review as reviews
from checkcord.services.connections import connection_key
from checkcord.storage import audit
from checkcord.storage.postgres import app_bypass_rls_context, tenant_context


class OwnedByOtherTenantError(Exception):
    pass


@dataclass(frozen=True)
class ConnectResult:
    installation_id: UUID
    created: bool
    generated_secret: str | None


def generate_secret() -> str:
    return secrets.token_urlsafe(32)


def read_secret(value: str | None, env_var: str, prompt: str) -> str:
    """The option, else the environment variable, else a hidden prompt on a terminal."""
    secret = value or os.environ.get(env_var)
    if not secret:
        if not sys.stdin.isatty():
            raise ValueError(f"{prompt} is required: pass it as an option or set {env_var}")
        secret = getpass.getpass(f"{prompt}: ")
    secret = secret.strip()
    if not secret:
        raise ValueError(f"{prompt} must not be empty")
    return secret


def add_rotate(p: argparse.ArgumentParser, what: str) -> None:
    p.add_argument(
        "--rotate-secret",
        action="store_true",
        help=f"issue a new {what} for an existing connection",
    )


def print_review_note(ctx: SetupContext, key: str) -> None:
    if ctx.review:
        print(f"In review: what arrives from {key} is held until `checkcord review release {key}`.")


def print_secret_once(label: str, secret: str | None) -> None:
    if secret is None:
        print(f"{label}: unchanged (pass --rotate-secret to issue a new one).")
        return
    print(f"{label} (shown once, not retrievable later):")
    print()
    print(f"    {secret}")
    print()


async def record_connection(
    conn: Any, tenant_id: UUID, provider: str, identifier: Any, created: bool, review: bool
) -> None:
    await reviews.mark_new(conn, tenant_id, connection_key(provider, identifier), review)
    await audit.record(
        conn,
        tenant_id,
        audit.CLI_ACTOR,
        "connection.added" if created else "connection.updated",
        params={"provider": provider, "identifier": str(identifier)},
    )


async def upsert_by_natural_key(
    engine: AsyncEngine,
    tenant_id: UUID,
    provider: str,
    model: Any,
    key_column: Any,
    key_value: str,
    values: dict[str, Any],
    create_secret: str,
    update_secret: str | None,
    review: bool,
) -> tuple[UUID, bool]:
    """Insert or update a row keyed by a provider identifier that is unique across tenants."""
    async with app_bypass_rls_context(engine) as conn:
        existing = (
            await conn.execute(
                select(model.id, model.tenant_id).where(key_column == key_value).with_for_update()
            )
        ).one_or_none()
        if existing is not None and existing.tenant_id != tenant_id:
            raise OwnedByOtherTenantError(key_value)

        if existing is None:
            installation_id = uuid4()
            await conn.execute(
                model.__table__.insert().values(
                    id=installation_id,
                    tenant_id=tenant_id,
                    webhook_secret=encrypt_secret(create_secret),
                    **{key_column.key: key_value},
                    **values,
                )
            )
            await record_connection(conn, tenant_id, provider, key_value, True, review)
            return installation_id, True

        changes = dict(values)
        if update_secret:
            changes["webhook_secret"] = encrypt_secret(update_secret)
        await conn.execute(
            update(model).where(model.id == existing.id).values(updated_at=func.now(), **changes)
        )
        await record_connection(conn, tenant_id, provider, key_value, False, review)
        return existing.id, False


async def upsert_per_tenant(
    engine: AsyncEngine,
    tenant_id: UUID,
    provider: str,
    model: Any,
    create_secret: str,
    update_secret: str | None,
    review: bool,
) -> tuple[UUID, bool]:
    """Reuse the tenant's existing row for providers addressed by installation id."""
    async with tenant_context(engine, tenant_id) as conn:
        existing_id = await conn.scalar(
            select(model.id)
            .where(model.tenant_id == tenant_id)
            .order_by(model.created_at)
            .limit(1)
            .with_for_update()
        )
        if existing_id is None:
            installation_id = uuid4()
            await conn.execute(
                model.__table__.insert().values(
                    id=installation_id,
                    tenant_id=tenant_id,
                    webhook_secret=encrypt_secret(create_secret),
                )
            )
            await record_connection(conn, tenant_id, provider, installation_id, True, review)
            return installation_id, True
        if update_secret:
            await conn.execute(
                update(model)
                .where(model.id == existing_id)
                .values(webhook_secret=encrypt_secret(update_secret), updated_at=func.now())
            )
        await record_connection(conn, tenant_id, provider, existing_id, False, review)
        return existing_id, False


def generated(installation_id: UUID, created: bool, secret: str, rotated: bool) -> ConnectResult:
    return ConnectResult(installation_id, created, secret if created or rotated else None)
