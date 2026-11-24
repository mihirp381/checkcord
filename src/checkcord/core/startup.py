import structlog
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.core.config import PROVIDER_ADDRESSES, Settings

logger = structlog.get_logger(__name__)


class StartupConfigError(RuntimeError):
    pass


def _security_problems(settings: Settings) -> list[str]:
    key = settings.fernet_master_key
    if not key:
        return ["FERNET_MASTER_KEY is not set; generate one with Fernet.generate_key()."]
    try:
        Fernet(key.encode())
    except ValueError:
        return ["FERNET_MASTER_KEY is not a valid Fernet key (32 url-safe base64-encoded bytes)."]
    return []


SHIPPED_DB_PASSWORDS = frozenset({"checkcord", "checkcord_app", "checkcord_owner"})


def default_database_password_problems(settings: Settings) -> list[str]:
    if settings.environment != "production":
        return []
    problems = []
    for name, url in (
        ("DATABASE_URL", settings.database_url),
        ("MIGRATION_DATABASE_URL", settings.migration_database_url),
    ):
        if not url:
            continue
        try:
            password = make_url(url).password
        except ArgumentError:
            continue
        if password in SHIPPED_DB_PASSWORDS:
            problems.append(
                f"{name} uses the shipped default password {password!r}; set "
                "POSTGRES_PASSWORD, CHECKCORD_APP_DB_PASSWORD and CHECKCORD_OWNER_DB_PASSWORD."
            )
    return problems


def sign_in_problems(settings: Settings) -> list[str]:
    if not settings.sign_in_enabled:
        return []
    problems = []
    if settings.default_tenant_id is None:
        problems.append("Sign-in needs DEFAULT_TENANT_ID: people sign in to that tenant.")
    redirect = settings.effective_oidc_redirect_uri
    if redirect is None:
        problems.append("Sign-in needs OIDC_REDIRECT_URI or PUBLIC_BASE_URL.")
    if settings.environment == "production":
        for name, url in (("OIDC_ISSUER", settings.oidc_issuer), ("OIDC_REDIRECT_URI", redirect)):
            if url and not url.startswith("https://"):
                problems.append(f"{name} must use https in production.")
    return problems


def provider_address_problems(settings: Settings) -> list[str]:
    if settings.environment != "production":
        return []
    return [
        f"{name.upper()} must use https in production."
        for name in PROVIDER_ADDRESSES
        if not getattr(settings, name).startswith("https://")
    ]


def validate_security_settings(settings: Settings) -> None:
    """Refuse to start outside development/test without the secrets that protect stored data."""
    if settings.environment == "test":
        return
    problems = (
        _security_problems(settings)
        + default_database_password_problems(settings)
        + sign_in_problems(settings)
        + provider_address_problems(settings)
    )
    if not problems:
        return
    if settings.environment == "development":
        logger.warning("startup.insecure_development_settings", problems=problems)
        return
    raise StartupConfigError(
        f"Refusing to start with ENVIRONMENT={settings.environment!r}: " + " ".join(problems)
    )


def validate_worker_settings(settings: Settings) -> None:
    """Refuse to start the worker without the API keys its pipeline calls need."""
    if settings.environment == "test":
        return
    if not settings.openai_api_key:
        raise StartupConfigError(
            "OPENAI_API_KEY is not set; the worker needs it for every chat-model call "
            "(set LLM_BASE_URL as well to use an OpenAI-compatible endpoint)."
        )
    if settings.embedding_provider == "voyage" and not settings.voyage_api_key:
        raise StartupConfigError("VOYAGE_API_KEY is not set but EMBEDDING_PROVIDER is 'voyage'.")


async def database_role_flags(engine: AsyncEngine) -> tuple[str, bool, bool]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT current_user, rolsuper, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
    return str(row[0]), bool(row[1]), bool(row[2])


def database_role_problem(role: str, superuser: bool, bypassrls: bool) -> str | None:
    if not (superuser or bypassrls):
        return None
    attrs = " and ".join(a for a, on in (("SUPERUSER", superuser), ("BYPASSRLS", bypassrls)) if on)
    return (
        f"DATABASE_URL connects as {role!r}, which has {attrs}: Postgres skips row-level "
        "security for it, so tenant isolation is off. Use a non-superuser application role "
        "(see deploy/postgres/roles.sql)."
    )


async def enforce_database_role(settings: Settings, engine: AsyncEngine) -> None:
    """Warn (or refuse in production) when the app's DB role is exempt from row-level security."""
    if settings.environment == "test":
        return
    problem = database_role_problem(*await database_role_flags(engine))
    if problem is None:
        return
    if settings.environment == "production":
        raise StartupConfigError(problem)
    logger.warning("startup.DATABASE_ROLE_BYPASSES_RLS", problem=problem)


async def enforce_embedding_column(settings: Settings, engine: AsyncEngine) -> None:
    """Refuse to start when the record embedding column and EMBEDDING_DIMENSION disagree."""
    from checkcord.storage import vector_column

    if settings.environment == "test":
        return
    async with engine.connect() as conn:
        problem = vector_column.mismatch(
            await vector_column.column_dimension(conn), settings.embedding_dimension
        )
    if problem:
        raise StartupConfigError(problem)
