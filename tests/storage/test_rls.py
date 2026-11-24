"""Integration tests for Postgres Row-Level Security (RLS) policies."""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.core.config import get_settings
from checkcord.storage.postgres import tenant_context

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_rls_policies() -> None:
    """Verify default deny, isolation, and bypass RLS policies on tables."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    # 0. Setup non-superuser role for RLS testing (idempotent)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "DO $$\n"
                "BEGIN\n"
                "    IF NOT EXISTS (\n"
                "        SELECT FROM pg_catalog.pg_roles WHERE rolname = 'rls_test_user'\n"
                "    ) THEN\n"
                "        CREATE ROLE rls_test_user WITH LOGIN;\n"
                "    END IF;\n"
                "END\n"
                "$$;"
            )
        )
        await conn.execute(text("GRANT USAGE ON SCHEMA public TO rls_test_user"))
        await conn.execute(
            text("GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO rls_test_user")
        )

    tenant_a = uuid4()
    tenant_b = uuid4()
    dummy_tenant = uuid4()

    # Generate unique identifiers for unique constraint columns
    team_id_a = f"T_{uuid4().hex[:8]}"
    ms_tenant_id_a = f"MS_{uuid4().hex[:8]}"

    # Generate IDs for test records
    ent_user_id_a = uuid4()
    ent_user_id_b = uuid4()
    chat_user_id_a = uuid4()
    chat_user_id_b = uuid4()
    col_id_a = uuid4()
    col_id_b = uuid4()
    skill_id_a = uuid4()
    skill_id_b = uuid4()
    slack_id_a = uuid4()
    msteams_id_a = uuid4()
    github_id_a = uuid4()
    jira_id_a = uuid4()
    datadog_id_a = uuid4()
    sentry_id_a = uuid4()

    try:
        # --- 1. INSERT TEST DATA AS SUPERUSER (RLS bypassed during insert) ---
        async with engine.begin() as conn:
            # Insert for Tenant A
            await conn.execute(
                text(
                    "INSERT INTO enterprise_users (id, tenant_id, primary_email, display_name) "
                    "VALUES (:id, :tenant_id, :email, :name)"
                ),
                {
                    "id": ent_user_id_a,
                    "tenant_id": tenant_a,
                    "email": "a@example.com",
                    "name": "User A",
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO chat_users (id, tenant_id, platform, native_id, display_name, "
                    "internal_id) "
                    "VALUES (:id, :tenant_id, 'slack', 'U_A', 'Chat A', :internal_id)"
                ),
                {
                    "id": chat_user_id_a,
                    "tenant_id": tenant_a,
                    "internal_id": ent_user_id_a,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO custom_collections (collection_id, tenant_id, name) "
                    "VALUES (:id, :tenant_id, 'Collection A')"
                ),
                {"id": col_id_a, "tenant_id": tenant_a},
            )
            await conn.execute(
                text(
                    "INSERT INTO skills (id, tenant_id, name, description, instructions) "
                    "VALUES (:id, :tenant_id, 'Skill A', 'Desc A', 'inst')"
                ),
                {"id": skill_id_a, "tenant_id": tenant_a},
            )

            # Insert for Tenant B
            await conn.execute(
                text(
                    "INSERT INTO enterprise_users (id, tenant_id, primary_email, display_name) "
                    "VALUES (:id, :tenant_id, :email, :name)"
                ),
                {
                    "id": ent_user_id_b,
                    "tenant_id": tenant_b,
                    "email": "b@example.com",
                    "name": "User B",
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO chat_users (id, tenant_id, platform, native_id, display_name, "
                    "internal_id) "
                    "VALUES (:id, :tenant_id, 'slack', 'U_B', 'Chat B', :internal_id)"
                ),
                {
                    "id": chat_user_id_b,
                    "tenant_id": tenant_b,
                    "internal_id": ent_user_id_b,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO custom_collections (collection_id, tenant_id, name) "
                    "VALUES (:id, :tenant_id, 'Collection B')"
                ),
                {"id": col_id_b, "tenant_id": tenant_b},
            )
            await conn.execute(
                text(
                    "INSERT INTO skills (id, tenant_id, name, description, instructions) "
                    "VALUES (:id, :tenant_id, 'Skill B', 'Desc B', 'inst')"
                ),
                {"id": skill_id_b, "tenant_id": tenant_b},
            )

            # Insert installations for Tenant A
            await conn.execute(
                text(
                    "INSERT INTO slack_installations (id, team_id, tenant_id, bot_token) "
                    "VALUES (:id, :team_id, :tenant_id, 'token_a')"
                ),
                {"id": slack_id_a, "team_id": team_id_a, "tenant_id": tenant_a},
            )
            await conn.execute(
                text(
                    "INSERT INTO msteams_installations (id, ms_tenant_id, tenant_id) "
                    "VALUES (:id, :ms_tenant_id, :tenant_id)"
                ),
                {"id": msteams_id_a, "ms_tenant_id": ms_tenant_id_a, "tenant_id": tenant_a},
            )
            await conn.execute(
                text(
                    "INSERT INTO github_installations "
                    "(id, repo_identifier, tenant_id, personal_access_token, webhook_secret) "
                    "VALUES (:id, 'org/repo_a', :tenant_id, 'pat_a', 'secret_a')"
                ),
                {"id": github_id_a, "tenant_id": tenant_a},
            )
            await conn.execute(
                text(
                    "INSERT INTO jira_installations "
                    "(id, jira_host, tenant_id, personal_access_token, webhook_secret) "
                    "VALUES (:id, 'host_a.atlassian.net', :tenant_id, 'pat_a', 'secret_a')"
                ),
                {"id": jira_id_a, "tenant_id": tenant_a},
            )
            await conn.execute(
                text(
                    "INSERT INTO datadog_installations "
                    "(id, tenant_id, webhook_secret) "
                    "VALUES (:id, :tenant_id, 'secret_a')"
                ),
                {"id": datadog_id_a, "tenant_id": tenant_a},
            )
            await conn.execute(
                text(
                    "INSERT INTO sentry_installations "
                    "(id, tenant_id, webhook_secret) "
                    "VALUES (:id, :tenant_id, 'secret_a')"
                ),
                {"id": sentry_id_a, "tenant_id": tenant_a},
            )

        # --- Scenario 1: Default-Deny without tenant context / dummy context ---
        # For enterprise_users, chat_users, custom_collections, and skills,
        # querying outside tenant_context (with app.current_tenant set to a dummy tenant)
        # as non-superuser must return 0 rows.
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL ROLE rls_test_user"))
            await conn.execute(text(f"SET LOCAL app.current_tenant = '{dummy_tenant}'"))

            res_ent = await conn.execute(text("SELECT id FROM enterprise_users"))
            assert len(res_ent.fetchall()) == 0

            res_chat = await conn.execute(text("SELECT id FROM chat_users"))
            assert len(res_chat.fetchall()) == 0

            res_col = await conn.execute(text("SELECT collection_id FROM custom_collections"))
            assert len(res_col.fetchall()) == 0

            res_skill = await conn.execute(text("SELECT id FROM skills"))
            assert len(res_skill.fetchall()) == 0

        # --- Scenario 2: Correct tenant context isolation ---
        # Querying under tenant_context(engine, tenant_a) as non-superuser
        # must see only Tenant A's data
        async with tenant_context(engine, tenant_a) as conn:
            await conn.execute(text("SET LOCAL ROLE rls_test_user"))
            res_ent = await conn.execute(text("SELECT id FROM enterprise_users"))
            rows_ent = res_ent.fetchall()
            assert len(rows_ent) == 1
            assert rows_ent[0][0] == ent_user_id_a

            res_chat = await conn.execute(text("SELECT id FROM chat_users"))
            rows_chat = res_chat.fetchall()
            assert len(rows_chat) == 1
            assert rows_chat[0][0] == chat_user_id_a

            res_col = await conn.execute(text("SELECT collection_id FROM custom_collections"))
            rows_col = res_col.fetchall()
            assert len(rows_col) == 1
            assert rows_col[0][0] == col_id_a

            res_skill = await conn.execute(text("SELECT id FROM skills"))
            rows_skill = res_skill.fetchall()
            assert len(rows_skill) == 1
            assert rows_skill[0][0] == skill_id_a

        # Querying under tenant_context(engine, tenant_b) as non-superuser
        # must see only Tenant B's data
        async with tenant_context(engine, tenant_b) as conn:
            await conn.execute(text("SET LOCAL ROLE rls_test_user"))
            res_ent = await conn.execute(text("SELECT id FROM enterprise_users"))
            rows_ent = res_ent.fetchall()
            assert len(rows_ent) == 1
            assert rows_ent[0][0] == ent_user_id_b

            res_chat = await conn.execute(text("SELECT id FROM chat_users"))
            rows_chat = res_chat.fetchall()
            assert len(rows_chat) == 1
            assert rows_chat[0][0] == chat_user_id_b

            res_col = await conn.execute(text("SELECT collection_id FROM custom_collections"))
            rows_col = res_col.fetchall()
            assert len(rows_col) == 1
            assert rows_col[0][0] == col_id_b

            res_skill = await conn.execute(text("SELECT id FROM skills"))
            rows_skill = res_skill.fetchall()
            assert len(rows_skill) == 1
            assert rows_skill[0][0] == skill_id_b

        # --- Scenario 3: Bypass RLS policy for installation tables ---
        # Queries on slack_installations and msteams_installations outside of tenant_context
        # (with app.current_tenant set to a dummy tenant) as non-superuser
        # must return 0 rows by default.
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL ROLE rls_test_user"))
            await conn.execute(text(f"SET LOCAL app.current_tenant = '{dummy_tenant}'"))
            res_slack = await conn.execute(text("SELECT id FROM slack_installations"))
            assert len(res_slack.fetchall()) == 0

            res_msteams = await conn.execute(text("SELECT id FROM msteams_installations"))
            assert len(res_msteams.fetchall()) == 0

            res_github = await conn.execute(text("SELECT id FROM github_installations"))
            assert len(res_github.fetchall()) == 0

            res_jira = await conn.execute(text("SELECT id FROM jira_installations"))
            assert len(res_jira.fetchall()) == 0

            res_datadog = await conn.execute(text("SELECT id FROM datadog_installations"))
            assert len(res_datadog.fetchall()) == 0

            res_sentry = await conn.execute(text("SELECT id FROM sentry_installations"))
            assert len(res_sentry.fetchall()) == 0

        # When we set app.bypass_rls = 'true' inside a transaction, the query
        # must succeed in fetching globally
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL ROLE rls_test_user"))
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))

            res_slack = await conn.execute(text("SELECT id FROM slack_installations"))
            rows_slack = res_slack.fetchall()
            assert len(rows_slack) >= 1
            assert any(r[0] == slack_id_a for r in rows_slack)

            res_msteams = await conn.execute(text("SELECT id FROM msteams_installations"))
            rows_msteams = res_msteams.fetchall()
            assert len(rows_msteams) >= 1
            assert any(r[0] == msteams_id_a for r in rows_msteams)

            res_github = await conn.execute(text("SELECT id FROM github_installations"))
            rows_github = res_github.fetchall()
            assert len(rows_github) >= 1
            assert any(r[0] == github_id_a for r in rows_github)

            res_jira = await conn.execute(text("SELECT id FROM jira_installations"))
            rows_jira = res_jira.fetchall()
            assert len(rows_jira) >= 1
            assert any(r[0] == jira_id_a for r in rows_jira)

            res_datadog = await conn.execute(text("SELECT id FROM datadog_installations"))
            rows_datadog = res_datadog.fetchall()
            assert len(rows_datadog) >= 1
            assert any(r[0] == datadog_id_a for r in rows_datadog)

            res_sentry = await conn.execute(text("SELECT id FROM sentry_installations"))
            rows_sentry = res_sentry.fetchall()
            assert len(rows_sentry) >= 1
            assert any(r[0] == sentry_id_a for r in rows_sentry)

    finally:
        # --- CLEAN UP GENERATED DATA ---
        async with engine.begin() as conn:
            # Delete in order of foreign key dependencies (superuser automatically bypasses RLS)
            await conn.execute(
                text("DELETE FROM chat_users WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM enterprise_users WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM custom_collections WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM skills WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM slack_installations WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM msteams_installations WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM github_installations WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM jira_installations WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM datadog_installations WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM sentry_installations WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )

        await engine.dispose()
