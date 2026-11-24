import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from redis.asyncio import Redis
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.core.config import PROVIDER_ADDRESSES, Settings, get_settings
from checkcord.core.crypto import decrypt_secret, is_encrypted
from checkcord.core.embedding_models import dimension_problem
from checkcord.core.oidc import OIDCClient, SignInError
from checkcord.core.startup import (
    _security_problems,
    database_role_flags,
    database_role_problem,
    default_database_password_problems,
    provider_address_problems,
)
from checkcord.storage.models import APIKey
from checkcord.storage.postgres import (
    app_bypass_rls_context,
    create_checkcord_engine,
    list_tenant_ids,
    tenant_context,
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    warn: bool = False

    @property
    def status(self) -> str:
        if not self.ok:
            return "FAIL"
        return "WARN" if self.warn else "PASS"


def find_alembic_ini() -> Path | None:
    candidates = [Path.cwd() / "alembic.ini", Path(__file__).resolve().parents[3] / "alembic.ini"]
    return next((p for p in candidates if p.is_file()), None)


def alembic_heads(ini: Path) -> set[str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return set(ScriptDirectory.from_config(Config(str(ini))).get_heads())


def _key_checks(settings: Settings) -> list[Check]:
    checks = [
        Check(
            "llm key",
            bool(settings.openai_api_key),
            "OPENAI_API_KEY set" if settings.openai_api_key else "OPENAI_API_KEY is not set",
        )
    ]
    if settings.embedding_provider == "voyage":
        present, var = bool(settings.voyage_api_key), "VOYAGE_API_KEY"
    else:
        present, var = bool(settings.openai_api_key), "OPENAI_API_KEY"
    checks.append(
        Check(
            "embedding key",
            present,
            f"{settings.embedding_provider}: {var} {'set' if present else 'is not set'}",
        )
    )
    size_problem = dimension_problem(
        settings.embedding_provider, settings.embedding_model, settings.embedding_dimension
    )
    checks.append(
        Check(
            "embedding model",
            size_problem is None,
            size_problem
            or f"{settings.embedding_model}, {settings.embedding_dimension}-wide vectors",
        )
    )
    problems = _security_problems(settings)
    if problems and settings.environment == "development":
        checks.append(
            Check(
                "encryption key",
                True,
                f"{problems[0]} Development mode uses a built-in key; never in production.",
                warn=True,
            )
        )
    else:
        checks.append(
            Check(
                "encryption key", not problems, problems[0] if problems else "FERNET_MASTER_KEY ok"
            )
        )
    password_problems = default_database_password_problems(settings)
    if password_problems:
        detail = " ".join(password_problems)
    elif settings.environment == "production":
        detail = "not a shipped default"
    else:
        detail = f"checked only in production (ENVIRONMENT={settings.environment})"
    checks.append(Check("db password", not password_problems, detail))
    checks.append(provider_address_check(settings))
    checks.append(meeting_judge_check(settings))
    return checks


def meeting_judge_check(settings: Settings) -> Check:
    if settings.meeting_judge_mode == "direct":
        return Check("meeting judge", True, "direct: verified in the transcript worker")
    base = (settings.llm_base_url or "https://api.openai.com/v1").rstrip("/")
    if not base.startswith("https://api.openai.com"):
        return Check(
            "meeting judge",
            False,
            f"MEETING_JUDGE_MODE=batch needs OpenAI's Batch API, but LLM_BASE_URL is {base}; "
            "set MEETING_JUDGE_MODE=direct",
        )
    if not settings.openai_api_key:
        return Check("meeting judge", False, "MEETING_JUDGE_MODE=batch needs OPENAI_API_KEY")
    return Check("meeting judge", True, "batch: verified through OpenAI's Batch API")


def provider_address_check(settings: Settings) -> Check:
    problems = provider_address_problems(settings)
    changed = [
        f"{name.upper()}={getattr(settings, name)}"
        for name in PROVIDER_ADDRESSES
        if getattr(settings, name) != Settings.model_fields[name].default
    ]
    if problems:
        return Check("provider addresses", False, " ".join(problems))
    if changed:
        return Check(
            "provider addresses", True, "not the default: " + ", ".join(changed), warn=True
        )
    return Check("provider addresses", True, "all defaults")


async def _tenants(engine: AsyncEngine, tenant_id: UUID | None) -> list[UUID]:
    return [tenant_id] if tenant_id is not None else await list_tenant_ids(engine)


async def _database_checks(settings: Settings, tenant_id: UUID | None = None) -> list[Check]:
    engine = create_checkcord_engine(settings.database_url, connect_args={"timeout": 5})
    try:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as e:
            return [Check("database", False, f"cannot connect: {type(e).__name__}: {e}")]
        checks = [Check("database", True, "reachable")]

        role, superuser, bypassrls = await database_role_flags(engine)
        problem = database_role_problem(role, superuser, bypassrls)
        checks.append(
            Check(
                "database role",
                problem is None,
                problem or f"{role}: not superuser, no BYPASSRLS (row-level security applies)",
            )
        )

        ini = find_alembic_ini()
        if ini is None:
            checks.append(Check("migrations", False, "alembic.ini not found"))
        else:
            heads = alembic_heads(ini)
            try:
                async with engine.connect() as conn:
                    current = {
                        r[0]
                        for r in await conn.execute(text("SELECT version_num FROM alembic_version"))
                    }
            except Exception:
                current = set()
            at_head = current == heads
            detail = f"at head {', '.join(sorted(heads))}"
            if not at_head:
                detail = (
                    f"database at {', '.join(sorted(current)) or 'nothing'}, "
                    f"head is {', '.join(sorted(heads))}; run `checkcord migrate`"
                )
            checks.append(Check("migrations", at_head, detail))

        checks.append(await embedding_column_check(engine, settings.embedding_dimension))

        audit_check = await audit_grants_check(engine)
        if audit_check is not None:
            checks.append(audit_check)

        try:
            async with app_bypass_rls_context(engine) as conn:
                admins = await conn.scalar(
                    select(func.count())
                    .select_from(APIKey)
                    .where(APIKey.active, APIKey.is_admin)
                    .where(True if tenant_id is None else APIKey.tenant_id == tenant_id)
                )
        except Exception as e:
            admins = 0
            checks.append(Check("admin key", False, f"cannot read api_keys: {type(e).__name__}"))
        else:
            checks.append(
                Check(
                    "admin key",
                    bool(admins),
                    f"{admins} active" if admins else "none active; run `checkcord init`",
                )
            )
        if tenant_id is None:
            checks.append(await credential_check(engine))
        checks.append(await event_pipeline_check(engine, tenant_id))
        checks.append(
            await audience_check(engine, settings.audience_refresh_grace_hours, tenant_id)
        )
        for optional in (
            await teams_subscription_check(engine, tenant_id),
            await review_check(engine, tenant_id),
            await unmatched_accounts_check(engine, tenant_id),
        ):
            if optional is not None:
                checks.append(optional)
        checks += await connection_audience_checks(engine, tenant_id)
        return checks
    finally:
        await engine.dispose()


async def embedding_column_check(engine: AsyncEngine, dimension: int) -> Check:
    from checkcord.storage import vector_column

    try:
        async with engine.connect() as conn:
            width = await vector_column.column_dimension(conn)
    except Exception as e:
        return Check("embedding column", False, f"cannot read: {type(e).__name__}: {e}")
    if width is None:
        return Check(
            "embedding column", False, "canonical_contexts not found; run `checkcord migrate`"
        )
    problem = vector_column.mismatch(width, dimension)
    return Check("embedding column", problem is None, problem or f"vector({width})")


def _model_ids_match(wanted: str, available: set[str]) -> bool:
    return wanted in available or f"{wanted}:latest" in available


async def model_checks(settings: Settings, http: httpx.AsyncClient | None = None) -> list[Check]:
    """The chat endpoint lists every configured model; the embedding model answers at full width."""
    from checkcord.llm.client import TASK_MODEL_SETTINGS
    from checkcord.pipeline.embedder import EmbeddingClient
    from checkcord.pipeline.embeddings_factory import build_embeddings_provider

    checks = []
    if settings.openai_api_key:
        base = (settings.llm_base_url or "https://api.openai.com/v1").rstrip("/")
        fields = [*TASK_MODEL_SETTINGS.values(), "redaction_llm_model"]
        fields += ["meeting_extraction_llm_model", "ci_failure_extraction_llm_model"]
        wanted = sorted({getattr(settings, f) for f in fields})
        own = http is None
        client = http or httpx.AsyncClient(timeout=10)
        try:
            response = await client.get(
                f"{base}/models", headers={"Authorization": f"Bearer {settings.openai_api_key}"}
            )
            response.raise_for_status()
            available = {str(m.get("id")) for m in response.json().get("data", [])}
        except Exception as e:
            checks.append(Check("llm models", False, f"{base}/models: {type(e).__name__}: {e}"))
        else:
            missing = [m for m in wanted if not _model_ids_match(m, available)]
            checks.append(
                Check(
                    "llm models",
                    not missing,
                    f"{base} does not list {', '.join(missing)}"
                    if missing
                    else f"{base} lists {', '.join(wanted)}",
                )
            )
        finally:
            if own:
                await client.aclose()

    provider = build_embeddings_provider(settings)
    if provider is not None:
        embedder = EmbeddingClient(
            provider, purpose="doctor", dimension=settings.embedding_dimension
        )
        try:
            vector = await asyncio.wait_for(embedder.embed_text("checkcord doctor"), timeout=30)
            detail = (
                f"answered with {settings.embedding_dimension}-wide vectors"
                if vector
                else "returned no vector"
            )
        except Exception as e:
            vector, detail = None, str(e) or type(e).__name__
        checks.append(Check("embedding endpoint", bool(vector), f"{embedder.model}: {detail}"))
    return checks


async def credential_check(engine: AsyncEngine) -> Check:
    from checkcord.cli.secrets import SECRET_COLUMNS

    try:
        async with app_bypass_rls_context(engine) as conn:
            for model, columns in SECRET_COLUMNS:
                cols = [getattr(model, c) for c in columns]
                rows = await conn.execute(select(*cols).where(or_(*(c.isnot(None) for c in cols))))
                for row in rows:
                    value = next((v for v in row if v and is_encrypted(v)), None)
                    if value is None:
                        continue
                    try:
                        decrypt_secret(value)
                    except Exception as e:
                        return Check("stored credentials", False, str(e))
                    return Check(
                        "stored credentials", True, f"{model.__tablename__} credential decrypts"
                    )
    except Exception as e:
        return Check("stored credentials", False, f"cannot read: {type(e).__name__}: {e}")
    return Check("stored credentials", True, "none encrypted yet")


async def event_pipeline_check(engine: AsyncEngine, tenant_id: UUID | None = None) -> Check:
    from checkcord.storage import event_pipeline

    totals = {event_pipeline.FAILED: 0, event_pipeline.PENDING: 0}
    try:
        for tenant in await _tenants(engine, tenant_id):
            async with tenant_context(engine, tenant) as conn:
                counts = await event_pipeline.count_unfinished(conn, tenant)
            for state, n in counts.items():
                totals[state] += n
    except Exception as e:
        return Check("events", False, f"cannot read: {type(e).__name__}: {e}")
    failed, retrying = totals[event_pipeline.FAILED], totals[event_pipeline.PENDING]
    if not failed and not retrying:
        return Check("events", True, "none failed or waiting to retry")
    parts = []
    if failed:
        parts.append(
            f"{failed} failed; run `checkcord events failed`, then `checkcord events retry`"
        )
    if retrying:
        parts.append(
            f"{retrying} waiting to retry the embedding step; check the model keys and "
            "`docker compose logs worker`"
        )
    return Check("events", True, "; ".join(parts), warn=True)


async def audience_check(
    engine: AsyncEngine, grace_hours: int, tenant_id: UUID | None = None
) -> Check:
    from datetime import UTC, datetime, timedelta

    from checkcord.storage import audiences

    cutoff = datetime.now(UTC) - timedelta(hours=grace_hours)
    total = stale = 0
    try:
        for tenant in await _tenants(engine, tenant_id):
            async with tenant_context(engine, tenant) as conn:
                rows = await audiences.list_rows(conn, tenant)
            total += len(rows)
            stale += sum(1 for r in rows if r["refreshed_at"] is None or r["refreshed_at"] < cutoff)
    except Exception as e:
        return Check("audiences", False, f"cannot read: {type(e).__name__}: {e}")
    if not stale:
        return Check("audiences", True, f"{total} stored, all refreshed within {grace_hours}h")
    return Check(
        "audiences",
        True,
        f"{stale} of {total} not refreshed within {grace_hours}h; run `checkcord audiences list`",
        warn=True,
    )


async def audit_grants_check(engine: AsyncEngine) -> Check | None:
    """The serving role must not be able to change audit entries; a stray GRANT can undo that."""
    try:
        async with engine.connect() as conn:
            owner = await conn.scalar(
                text(
                    "SELECT pg_get_userbyid(relowner) = current_user "
                    "OR (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) "
                    "FROM pg_class WHERE oid = to_regclass('audit_log_entries')"
                )
            )
            if owner is None or owner:
                return None
            writable = await conn.scalar(
                text(
                    "SELECT has_table_privilege(current_user, 'audit_log_entries', 'UPDATE') "
                    "OR has_table_privilege(current_user, 'audit_log_entries', 'DELETE')"
                )
            )
    except Exception as err:
        return Check("audit grants", False, f"could not read grants: {err}")
    if writable:
        return Check(
            "audit grants",
            False,
            "this role can change audit entries; run "
            "`REVOKE UPDATE, DELETE, TRUNCATE ON audit_log_entries FROM checkcord_app` "
            "as checkcord_owner",
        )
    return Check("audit grants", True, "audit entries can be added, not changed")


async def teams_subscription_check(
    engine: AsyncEngine, tenant_id: UUID | None = None
) -> Check | None:
    """A line only when Teams meetings is connected: each Graph subscription is live."""
    from datetime import UTC, datetime

    from checkcord.storage.models import MsTeamsMeetingInstallation as Teams

    try:
        async with app_bypass_rls_context(engine) as conn:
            rows = (
                await conn.execute(
                    select(Teams.subscription_expires_at, Teams.subscription_error).where(
                        True if tenant_id is None else Teams.tenant_id == tenant_id
                    )
                )
            ).all()
    except Exception as e:
        return Check("teams subscription", False, f"cannot read: {type(e).__name__}: {e}")
    if not rows:
        return None
    now = datetime.now(UTC)
    live = [expires for expires, _ in rows if expires is not None and expires > now]
    errors = sorted({error for _, error in rows if error})
    if len(live) == len(rows):
        detail = f"{len(rows)} live, next expiry {min(live):%Y-%m-%d %H:%M} UTC"
        if errors:
            return Check(
                "teams subscription", True, f"{detail}; last renewal: {errors[0]}", warn=True
            )
        return Check("teams subscription", True, detail)
    reason = f"; {errors[0]}" if errors else "; the API creates it within an hour"
    return Check(
        "teams subscription", False, f"{len(rows) - len(live)} of {len(rows)} not live{reason}"
    )


async def review_check(engine: AsyncEngine, tenant_id: UUID | None = None) -> Check | None:
    """A line only when something is in review or held."""
    from checkcord.services import review

    connections = held = 0
    try:
        for tenant in await _tenants(engine, tenant_id):
            async with tenant_context(engine, tenant, include_held=True) as conn:
                counts = await review.held_counts(conn, tenant)
            connections += len(counts)
            held += sum(counts.values())
    except Exception as e:
        return Check("review", False, f"cannot read: {type(e).__name__}: {e}")
    if not connections:
        return None
    return Check(
        "review",
        True,
        f"{connections} connection(s) in review or holding material, {held} held record(s); "
        "run `checkcord review list`",
        warn=True,
    )


async def unmatched_accounts_check(
    engine: AsyncEngine, tenant_id: UUID | None = None
) -> Check | None:
    """A line only when a restricted channel or repository lists an account with no person."""
    from checkcord.services import accounts

    try:
        total = sum(
            [
                await accounts.unmatched_in_restricted(engine, t)
                for t in await _tenants(engine, tenant_id)
            ]
        )
    except Exception as e:
        return Check("accounts", False, f"cannot read: {type(e).__name__}: {e}")
    if not total:
        return None
    return Check(
        "accounts",
        True,
        f"{total} account(s) in restricted channels or repositories are not matched to a "
        "person; run `checkcord accounts list --unmatched`",
        warn=True,
    )


async def connection_audience_checks(
    engine: AsyncEngine, tenant_id: UUID | None = None
) -> list[Check]:
    """One line per admin-audience connection whose live records nobody may read."""
    from checkcord.services import connection_audiences

    found: list[Check] = []
    try:
        for tenant in await _tenants(engine, tenant_id):
            for key, n in sorted(
                (await connection_audiences.nobody_counts(engine, tenant)).items()
            ):
                found.append(
                    Check(
                        f"audience {key}",
                        True,
                        f"{n} record(s) from {key} are readable by nobody; set an audience "
                        "with `checkcord connections audience`",
                        warn=True,
                    )
                )
    except Exception as e:
        return [Check("connection audiences", False, f"cannot read: {type(e).__name__}: {e}")]
    return found


async def dead_letter_check(
    redis: Redis, tenant_id: UUID | None = None, default_tenant: UUID | None = None
) -> Check:
    from checkcord.bus.dead_letter import list_streams
    from checkcord.services import dead_letters

    try:
        if tenant_id is None:
            streams: list[Any] = await list_streams(redis)
        else:
            streams = await dead_letters.streams(redis, tenant_id, default_tenant)
    except Exception as e:
        return Check("dead letters", False, f"cannot read: {type(e).__name__}: {e}")
    if not streams:
        return Check("dead letters", True, "none")
    total = sum(s.entries for s in streams)
    names = ", ".join(s.name for s in streams)
    return Check(
        "dead letters",
        True,
        f"{total} in {names}; run `checkcord dlq list`, then `checkcord dlq show STREAM`",
        warn=True,
    )


BUFFER_GRACE_SECONDS = 120


async def discussion_check(redis: Redis, tenant_id: UUID | None = None) -> Check:
    from checkcord.storage import redis_trees

    try:
        found = await redis_trees.discussion_counts(redis, tenant_id)
        buffered = await redis_trees.buffered_counts(redis, tenant_id)
    except Exception as e:
        return Check("discussions", False, f"cannot read: {type(e).__name__}: {e}")
    notes = []
    late = (get_settings().chat_buffer_timeout_seconds + BUFFER_GRACE_SECONDS) / 60
    if buffered["buffered"] and buffered["buffered_oldest_minutes"] > late:
        notes.append(
            f"{buffered['buffered']} chat message(s) waiting to be grouped "
            f"(oldest {buffered['buffered_oldest_minutes']:g} min)"
        )
    if found["error"] and found["error_minutes"] > 10:
        notes.append(
            f"waiting {found['error_minutes']:g} min after a failed extraction: {found['error']}"
        )
    if found["failed"]:
        notes.append(f"{found['failed']} failed; run `checkcord events failed`")
    if notes:
        return Check("discussions", True, "; ".join(notes), warn=True)
    return Check(
        "discussions",
        True,
        f"{found['waiting']} waiting, {buffered['buffered']} message(s) to group, none failed",
    )


async def _redis_checks(settings: Settings, tenant_id: UUID | None = None) -> list[Check]:
    client = Redis.from_url(settings.redis_url, socket_connect_timeout=5, socket_timeout=5)
    try:
        await client.ping()
    except Exception as e:
        return [Check("redis", False, f"cannot connect: {type(e).__name__}: {e}")]
    else:
        return [
            Check("redis", True, "reachable"),
            await dead_letter_check(client, tenant_id, settings.default_tenant_id),
            await discussion_check(client, tenant_id),
        ]
    finally:
        await client.aclose()


def _retention_check(settings: Settings) -> Check:
    def kept(days: int | None) -> str:
        return "forever" if days is None else f"{days} days"

    return Check(
        "retention",
        True,
        f"events {kept(settings.event_retention_days)}, "
        f"audit entries {kept(settings.audit_retention_days)}",
    )


def connectors_check() -> Check:
    """Every connector loaded, including those installed as packages."""
    from checkcord import connectors

    found = connectors.connectors()
    errors = connectors.load_errors()
    if errors:
        return Check("connectors", False, f"cannot load {'; '.join(errors)}")
    by_status = {s: sorted(c.name for c in found if c.status == s) for s in connectors.STATUSES}
    detail = "; ".join(f"{s}: {', '.join(names)}" for s, names in by_status.items() if names)
    return Check("connectors", True, detail)


async def sign_in_check(settings: Settings, client: OIDCClient | None = None) -> Check:
    if not settings.sign_in_enabled:
        return Check("sign-in", True, "not configured; API keys only")
    if settings.default_tenant_id is None:
        return Check("sign-in", False, "OIDC is configured but DEFAULT_TENANT_ID is not set")
    if settings.effective_oidc_redirect_uri is None:
        return Check("sign-in", False, "set OIDC_REDIRECT_URI or PUBLIC_BASE_URL")
    own = client is None
    oidc = client or OIDCClient(settings)
    try:
        meta = await oidc.metadata(refresh=True)
    except SignInError as err:
        return Check("sign-in", False, f"issuer {settings.oidc_issuer} not usable: {err}")
    finally:
        if own:
            await oidc.aclose()
    return Check(
        "sign-in",
        True,
        f"issuer {meta.issuer} reachable; redirect URI {settings.effective_oidc_redirect_uri}",
    )


async def run_checks(
    settings: Settings, tenant_id: UUID | None = None, oidc: OIDCClient | None = None
) -> list[Check]:
    """Every check; with ``tenant_id``, the per-tenant ones look only at that tenant."""
    checks = await _database_checks(settings, tenant_id)
    checks.extend(await _redis_checks(settings, tenant_id))
    checks.extend(_key_checks(settings))
    checks.extend(await model_checks(settings))
    checks.append(await sign_in_check(settings, oidc))
    checks.append(_retention_check(settings))
    checks.append(connectors_check())
    return checks
