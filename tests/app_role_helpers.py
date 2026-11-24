import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import asyncpg
from sqlalchemy.engine import make_url

from checkcord.core.config import get_settings

ROLES_SQL = Path(__file__).resolve().parents[1] / "deploy" / "postgres" / "roles.sql"


async def create_app_role() -> tuple[str, str]:
    """A throwaway login role in checkcord_app; returns (role, database URL for it)."""
    admin_url = make_url(get_settings().database_url)
    role, password = f"checkcord_app_test_{uuid4().hex[:8]}", "app-role-test"
    admin = await asyncpg.connect(admin_url.set(drivername="postgresql").render_as_string(False))
    try:
        await admin.execute(ROLES_SQL.read_text())
        await admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' IN ROLE checkcord_app")
    finally:
        await admin.close()
    return role, admin_url.set(username=role, password=password).render_as_string(False)


async def drop_app_role(role: str) -> None:
    admin_url = make_url(get_settings().database_url)
    admin = await asyncpg.connect(admin_url.set(drivername="postgresql").render_as_string(False))
    try:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = $1", role
        )
        await admin.execute(f"DROP ROLE IF EXISTS {role}")
    finally:
        await admin.close()


@contextmanager
def app_database_url(url: str) -> Iterator[None]:
    """Point get_settings().database_url at ``url`` so the app connects as that role."""
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()
