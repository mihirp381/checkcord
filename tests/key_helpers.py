import asyncio
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.core.config import get_settings
from checkcord.core.crypto import hash_api_key


@dataclass(frozen=True)
class KeySpec:
    token: str
    tenant_id: UUID
    admin: bool = False
    user_email: str | None = None
    name: str = "test key"


@asynccontextmanager
async def seeded_keys(specs: Iterable[KeySpec]) -> AsyncIterator[dict[str, UUID]]:
    """Insert api_keys rows (and bound enterprise users); yields {email: user_id}."""
    specs = list(specs)
    users: dict[tuple[UUID, str], UUID] = {}
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            for spec in specs:
                user_id = None
                if spec.user_email:
                    key = (spec.tenant_id, spec.user_email)
                    if key not in users:
                        users[key] = uuid4()
                        await conn.execute(
                            text(
                                "INSERT INTO enterprise_users "
                                "(id, tenant_id, primary_email, display_name, active) "
                                "VALUES (:id, :tid, :email, :email, true)"
                            ),
                            {"id": users[key], "tid": spec.tenant_id, "email": spec.user_email},
                        )
                    user_id = users[key]
                await conn.execute(
                    text(
                        "INSERT INTO api_keys "
                        "(id, hashed_token, tenant_id, name, active, is_admin, user_id) "
                        "VALUES (gen_random_uuid(), :hashed, :tid, :name, true, :admin, :uid)"
                    ),
                    {
                        "hashed": hash_api_key(spec.token),
                        "tid": spec.tenant_id,
                        "name": spec.name,
                        "admin": spec.admin,
                        "uid": user_id,
                    },
                )
        by_email = {email: uid for (_, email), uid in users.items()}
        assert len(by_email) == len(users), "use distinct emails across tenants"
        yield by_email
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            for spec in specs:
                await conn.execute(
                    text("DELETE FROM api_keys WHERE hashed_token = :h"),
                    {"h": hash_api_key(spec.token)},
                )
            for uid in users.values():
                await conn.execute(text("DELETE FROM enterprise_users WHERE id = :id"), {"id": uid})
        await engine.dispose()


@contextmanager
def tenant_keys(tokens: dict[str, str]) -> Iterator[dict[str, str]]:
    """Seed non-admin api_keys for {token: tenant_id} around a sync module fixture."""
    manager = seeded_keys(KeySpec(token, UUID(tenant)) for token, tenant in tokens.items())
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(manager.__aenter__())
        try:
            yield tokens
        finally:
            loop.run_until_complete(manager.__aexit__(None, None, None))
    finally:
        loop.close()


async def seed_context(
    tenant_id: UUID,
    summary: str,
    *,
    public: bool,
    allowed: Iterable[UUID] = (),
    context_type: str = "adr",
) -> UUID:
    from checkcord.storage.postgres import PostgresEventStore, tenant_context

    access = {
        "is_public": public,
        "visibility": "public" if public else "private",
        "acl_type": "custom",
        "allowed_identity_ids": [str(u) for u in allowed],
        "admin_overrides": [],
    }
    engine = create_async_engine(get_settings().database_url)
    try:
        async with tenant_context(engine, tenant_id) as conn:
            return await PostgresEventStore(conn).save_canonical_context(
                tenant_id=tenant_id,
                summary=summary,
                confidence_score=9.0,
                access_control=access,
                context_type=context_type,
            )
    finally:
        await engine.dispose()


async def delete_contexts(*tenant_ids: UUID) -> None:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            for tid in tenant_ids:
                await conn.execute(
                    text("DELETE FROM canonical_contexts WHERE tenant_id = :t"), {"t": tid}
                )
    finally:
        await engine.dispose()


async def everyone_reads(tenant_id: UUID, *connections: str) -> None:
    """Let every person read what these admin-audience connections ingest."""
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            for connection in connections:
                await conn.execute(
                    text(
                        "INSERT INTO connection_audiences (tenant_id, connection, mode) "
                        "VALUES (:t, :c, 'everyone')"
                    ),
                    {"t": tenant_id, "c": connection},
                )
    finally:
        await engine.dispose()
