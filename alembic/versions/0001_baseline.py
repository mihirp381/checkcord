"""Baseline schema."""

import hashlib
import json
import logging
from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    _initial_schema()
    _chat_integration()
    _tenant_rls_and_identity()
    _github_integration()
    _jira_integration()
    _monitoring_integration()
    _jira_project_keys()
    _meeting_integration()
    _skills_table_and_agent_metadata()
    _modify_agent_ledgers_table()
    _canonical_contexts()
    _ast_nodes_schema()
    _last_accessed_to_ast_context_edges()
    _is_locked_to_canonical_contexts()
    _deprecated_to_ast_edge_status()
    _enriched_metadata_columns_to_events()
    _meeting_extraction_tables()
    _audit_status_to_extractions()
    _api_keys_and_installations()
    _jira_installs_updated_at()
    _access_control_to_canonical_contexts()
    _per_user_access_control_rls()
    _scope_repo_identifier_to_canonical_contexts()
    _context_scope_audit_log()
    _content_hash_and_last_verified_at_to_canonical_contexts()
    _audit_log_entries_and_api_key_is_admin()
    _severity_to_canonical_contexts()
    _summary_tsv_to_canonical_contexts()
    _context_repo_scopes()
    _embedding_hnsw_index()
    _workflow_definitions()
    _ci_failure_context_links()
    _generalize_context_repo_scopes()
    _context_type_to_canonical_contexts()
    _user_repo_affinity()
    _typed_scope_rows_to_audit_log()
    _events_default_partition()
    _app_role_support()
    _api_key_user_binding()
    _pipeline_state_to_events()
    _audit_log_reads_and_admin_actions()
    _audience_containers()
    _event_deletion()
    _connection_review()
    _stored_redaction_and_model_usage()
    _sign_in_sessions()
    _scheduled_jobs_and_teams_subscription()
    _record_lifecycle()
    _connection_scope()
    _scim_groups()
    _drop_empty_event_partitions()
    _connector_installations()
    _record_update_excerpts()
    _task_poll_cursors()
    _accounts_and_base_events()
    _removed_sources()


def downgrade() -> None:
    raise NotImplementedError("downgrade below the baseline is not supported")


_DEFAULT_ACCESS_CONTROL_JSON = (
    '{"is_public": false, "visibility": "private", "acl_type": "open", '
    '"allowed_identity_ids": [], "source_group_id": null, "inherited": false, '
    '"admin_overrides": []}'
)


_TENANT = "nullif(current_setting('app.current_tenant', true), '')::UUID"


_USER = "nullif(current_setting('app.current_user_id', true), '')"


_TENANT_POLICIES = {
    "canonical_contexts": "tenant_isolation_canonical",
    "context_event_links": "tenant_isolation_context_links",
    "context_repo_scopes": "tenant_isolation_context_repo_scopes",
    "context_scope_audit_log": "tenant_isolation_context_scope_audit_log",
    "user_repo_affinity": "tenant_isolation_user_repo_affinity",
}


_USER_POLICIES_NEW = {
    ("canonical_contexts", "user_access_control_canonical"): f"""
        {_USER} IS NULL
        OR COALESCE((access_control->>'is_public')::boolean, false)
        OR (access_control->'allowed_identity_ids') ? {_USER}
        OR (access_control->'admin_overrides') ? {_USER}
    """,
    ("events", "user_access_control_events"): f"""
        {_USER} IS NULL
        OR COALESCE((access_control->>'is_public')::boolean, false)
        OR {_USER}::uuid = ANY(access_allowed_ids)
        OR (access_control->'admin_overrides') ? {_USER}
    """,
    ("user_repo_affinity", "user_access_control_user_repo_affinity"): f"""
        {_USER} IS NULL OR user_id = {_USER}::uuid
    """,
}


_ENSURE_PARTITION = r"""
CREATE OR REPLACE FUNCTION checkcord_ensure_events_partition(p_name text, p_start date, p_end date)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    bounds text := format('FOR VALUES FROM (%L) TO (%L)', p_start, p_end);
    cols text;
    moved bigint := 0;
BEGIN
    IF p_name !~ '^events_[0-9]{4}_w[0-9]{2}$' OR p_end <= p_start THEN
        RAISE EXCEPTION 'invalid events partition % [%, %)', p_name, p_start, p_end;
    END IF;
    IF to_regclass(p_name) IS NOT NULL THEN
        RETURN 0;
    END IF;

    IF to_regclass('events_default') IS NULL THEN
        EXECUTE format('CREATE TABLE %I PARTITION OF events %s', p_name, bounds);
    ELSE
        -- Rows already in the default partition for this range would block CREATE/ATTACH.
        LOCK TABLE events_default IN EXCLUSIVE MODE;
        EXECUTE format(
            'CREATE TABLE %I (LIKE events INCLUDING DEFAULTS INCLUDING CONSTRAINTS)', p_name
        );
        SELECT string_agg(quote_ident(attname), ', ' ORDER BY attnum) INTO cols
        FROM pg_attribute
        WHERE attrelid = 'events'::regclass AND attnum > 0 AND NOT attisdropped;
        EXECUTE format(
            'WITH moved AS (DELETE FROM events_default WHERE happened_at >= %L '
            'AND happened_at < %L RETURNING %s) INSERT INTO %I (%s) SELECT %s FROM moved',
            p_start, p_end, cols, p_name, cols, cols
        );
        GET DIAGNOSTICS moved = ROW_COUNT;
        EXECUTE format('ALTER TABLE events ATTACH PARTITION %I %s', p_name, bounds);
    END IF;

    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', p_name);
    RETURN moved;
END
$$;
"""


_PARTITIONS_RLS = """
DO $$
DECLARE part regclass;
BEGIN
    FOR part IN SELECT inhrelid::regclass FROM pg_inherits WHERE inhparent = 'events'::regclass
    LOOP
        EXECUTE format('ALTER TABLE %s {action} ROW LEVEL SECURITY', part);
    END LOOP;
END
$$;
"""


_TENANT_OR_BYPASS = (
    "tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID "
    "OR current_setting('app.bypass_rls', true) = 'true'"
)


_NEW_INDEXES = {
    "ix_audit_log_entries_tenant_time": "(tenant_id, created_at DESC, id DESC)",
    "ix_audit_log_entries_tenant_user": "(tenant_id, user_id, created_at DESC)",
    "ix_audit_log_entries_tenant_key": "(tenant_id, key_id, created_at DESC)",
    "ix_audit_log_entries_tenant_action": "(tenant_id, action, created_at DESC)",
    "ix_audit_log_entries_record_ids": "USING GIN (record_ids)",
}


_OLD_INDEXES = {
    "ix_audit_log_entries_tenant_id": "(tenant_id)",
    "ix_audit_log_entries_event_type": "(event_type)",
    "ix_audit_log_entries_created_at": "(created_at)",
}


_BACKFILL = """
UPDATE events SET audience_container = CASE
    WHEN source_platform = 'github'
        THEN COALESCE(code_metadata->>'repository', metadata->>'repository')
    ELSE chat_metadata->>'channel_id'
END
WHERE (source_platform = 'github'
        AND COALESCE(code_metadata->>'repository', metadata->>'repository') IS NOT NULL)
   OR (source_platform IN ('slack', 'ms_teams')
        AND source_id LIKE 'chat-tree:%'
        AND chat_metadata->>'channel_id' IS NOT NULL)
"""


_SEED = """
INSERT INTO audience_containers (tenant_id, platform, container)
SELECT DISTINCT tenant_id, source_platform, audience_container
FROM events WHERE audience_container IS NOT NULL
ON CONFLICT DO NOTHING
"""


_BACKFILL_ORIGIN = """
UPDATE canonical_contexts c SET origin_event_id = first.event_id
FROM (
    SELECT DISTINCT ON (canonical_id) canonical_id, event_id
    FROM context_event_links
    ORDER BY canonical_id, created_at, event_id
) first
WHERE first.canonical_id = c.id AND c.origin_event_id IS NULL
"""


_HELD = "held_connection IS NULL OR current_setting('app.include_held', true) = 'true'"


_SEED_HELD = """
INSERT INTO connection_reviews (tenant_id, connection)
SELECT tenant_id, 'github:' || repo_identifier FROM github_installations
UNION SELECT tenant_id, 'jira:' || jira_host FROM jira_installations
UNION SELECT tenant_id, 'datadog:' || id::text FROM datadog_installations
UNION SELECT tenant_id, 'sentry:' || id::text FROM sentry_installations
UNION SELECT tenant_id, 'zoom:' || zoom_account_id FROM zoom_installations
UNION SELECT tenant_id, 'teams-meetings:' || ms_tenant_id FROM msteams_meeting_installations
UNION SELECT tenant_id, 'slack:' || team_id FROM slack_installations
UNION SELECT tenant_id, 'teams:' || ms_tenant_id FROM msteams_installations
UNION SELECT tenant_id, 'linear:' || linear_organization_id FROM linear_installations
UNION SELECT tenant_id, 'notion:' || notion_workspace_id FROM notion_installations
UNION SELECT DISTINCT tenant_id, 'custom:push' FROM api_keys
UNION SELECT DISTINCT tenant_id, connection FROM events WHERE connection IS NOT NULL
UNION SELECT DISTINCT tenant_id, 'github:' || audience_container FROM events
    WHERE source_platform = 'github' AND audience_container IS NOT NULL
ON CONFLICT DO NOTHING
"""


_CHECK_LINKS = """
CREATE FUNCTION checkcord_check_held_links() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    prev_held text := current_setting('app.include_held', true);
    prev_user text := current_setting('app.current_user_id', true);
    mixed boolean;
BEGIN
    PERFORM set_config('app.include_held', 'true', true);
    PERFORM set_config('app.current_user_id', '', true);
    IF TG_ARGV[0] = 'link' THEN
        PERFORM 1 FROM events e, canonical_contexts c
        WHERE e.tenant_id = NEW.tenant_id AND e.event_id = NEW.event_id
          AND c.id = NEW.canonical_id
          AND e.held_connection IS DISTINCT FROM c.held_connection;
    ELSIF TG_ARGV[0] = 'record' THEN
        PERFORM 1 FROM context_event_links l
        JOIN events e ON e.tenant_id = l.tenant_id AND e.event_id = l.event_id
        WHERE l.canonical_id = NEW.id
          AND e.held_connection IS DISTINCT FROM NEW.held_connection;
    ELSE
        PERFORM 1 FROM context_event_links l
        JOIN canonical_contexts c ON c.id = l.canonical_id
        WHERE l.tenant_id = NEW.tenant_id AND l.event_id = NEW.event_id
          AND c.held_connection IS DISTINCT FROM NEW.held_connection;
    END IF;
    mixed := FOUND;
    PERFORM set_config('app.include_held', coalesce(prev_held, ''), true);
    PERFORM set_config('app.current_user_id', coalesce(prev_user, ''), true);
    IF mixed THEN
        RAISE EXCEPTION 'held and live material cannot be linked'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$$
"""


_TRIGGERS = (
    ("held_links_link", "context_event_links", "AFTER INSERT OR UPDATE", "", "link"),
    (
        "held_links_record",
        "canonical_contexts",
        "AFTER UPDATE OF held_connection",
        "WHEN (OLD.held_connection IS DISTINCT FROM NEW.held_connection) ",
        "record",
    ),
    (
        "held_links_event",
        "events",
        "AFTER UPDATE OF held_connection",
        "WHEN (OLD.held_connection IS DISTINCT FROM NEW.held_connection) ",
        "event",
    ),
)


_RETIRED = "retired_at IS NULL OR current_setting('app.include_retired', true) = 'true'"


_SCOPE_TABLES = ("connection_scopes", "connection_scope_skips")


_SCIM_TABLES = ("scim_groups", "scim_group_members")


_DROP_EMPTY = r"""
CREATE OR REPLACE FUNCTION checkcord_drop_empty_events_partitions(p_before date)
RETURNS SETOF text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    part text;
    has_rows boolean;
BEGIN
    FOR part IN
        SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
        WHERE i.inhparent = 'events'::regclass AND c.relname ~ '^events_[0-9]{4}_w[0-9]{2}$'
          AND to_date(substr(c.relname, 8, 4) || '-' || substr(c.relname, 14, 2) || '-1',
                      'IYYY-IW-ID') + 7 <= p_before
        ORDER BY c.relname
    LOOP
        EXECUTE format('LOCK TABLE events, %I IN ACCESS EXCLUSIVE MODE', part);
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I)', part) INTO has_rows;
        IF NOT has_rows THEN
            EXECUTE format('DROP TABLE %I', part);
            RETURN NEXT part;
        END IF;
    END LOOP;
END
$$;
"""


_CONNECTOR_TABLE = "connector_installations"


_POLL_CURSOR_TABLES = ("jira_installations", "notion_installations", "linear_installations")


_BACKFILL_BASE = """
UPDATE canonical_contexts c SET base_event_id = first.event_id
FROM (
    SELECT DISTINCT ON (canonical_id) canonical_id, event_id
    FROM context_event_links
    ORDER BY canonical_id, created_at, event_id
) first
WHERE first.canonical_id = c.id AND c.base_event_id IS NULL
"""


_FROM_BASE = """
UPDATE canonical_contexts c SET access_control = jsonb_build_object(
    'is_public', p.public,
    'visibility', CASE WHEN p.public THEN 'public' ELSE 'restricted' END,
    'acl_type', 'open',
    'allowed_identity_ids', CASE WHEN p.public THEN '[]'::jsonb
        ELSE COALESCE((SELECT jsonb_agg(u::text ORDER BY u::text)
                       FROM unnest(p.ids) u), '[]'::jsonb) END,
    'source_group_id', NULL,
    'inherited', false,
    'admin_overrides', COALESCE(c.access_control->'admin_overrides', '[]'::jsonb)
)
FROM (
    SELECT e.event_id, e.tenant_id, e.access_allowed_ids AS ids,
           COALESCE((e.access_control->>'is_public')::boolean, false) AS public
    FROM events e
) p
WHERE p.event_id = c.base_event_id AND p.tenant_id = c.tenant_id
"""


def _if_app_role(statement: str) -> str:
    return f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'checkcord_app') THEN
                {statement}
            END IF;
        END
        $$;
    """


def _bypass() -> None:
    op.execute(sa.text("SELECT set_config('app.bypass_rls', 'true', true)"))


def _initial_schema() -> None:

    op.execute(
        sa.text("""
        CREATE TABLE events (
            event_id            UUID            NOT NULL,
            tenant_id           UUID            NOT NULL,
            parent_event_id     UUID,
            fingerprint         VARCHAR         NOT NULL,
            schema_version      INTEGER         NOT NULL DEFAULT 1,

            -- Source Origin
            source_platform     VARCHAR         NOT NULL,
            source_type         VARCHAR         NOT NULL,
            event_action        VARCHAR         NOT NULL DEFAULT 'created',
            source_id           VARCHAR         NOT NULL,
            version             INTEGER         NOT NULL DEFAULT 1,

            -- Temporal
            happened_at         TIMESTAMPTZ     NOT NULL,
            ingested_at         TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

            -- Payload (text)
            content_raw         TEXT            NOT NULL,
            content_clean       TEXT            NOT NULL,

            -- Governance & State
            signal_status       VARCHAR         NOT NULL DEFAULT 'unprocessed',
            retention_class     VARCHAR         NOT NULL DEFAULT 'standard',

            -- Nested JSONB payloads
            author              JSONB           NOT NULL,
            access_control      JSONB           NOT NULL,
            source_raw_payload  JSONB           NOT NULL DEFAULT '{}',
            code_metadata       JSONB,
            task_metadata       JSONB,
            document_metadata   JSONB,
            entities            JSONB           NOT NULL DEFAULT '[]',
            metadata            JSONB           NOT NULL DEFAULT '{}',

            -- Top-level arrays for GIN indexing (NO monoliths)
            access_allowed_ids  UUID[]          NOT NULL DEFAULT '{}',
            code_files_changed  VARCHAR[]       NOT NULL DEFAULT '{}',

            PRIMARY KEY (event_id, happened_at)
        ) PARTITION BY RANGE (happened_at);
        """)
    )

    op.execute(
        sa.text("""
        CREATE UNIQUE INDEX uq_events_tenant_fingerprint
        ON events (tenant_id, fingerprint, happened_at);
        """)
    )

    op.execute(sa.text("CREATE INDEX ix_events_tenant_id ON events (tenant_id);"))
    op.execute(
        sa.text("CREATE INDEX ix_events_tenant_happened ON events (tenant_id, happened_at DESC);")
    )
    op.execute(
        sa.text("CREATE INDEX ix_events_tenant_platform ON events (tenant_id, source_platform);")
    )
    op.execute(sa.text("CREATE INDEX ix_events_tenant_source_id ON events (tenant_id, source_id);"))

    op.execute(
        sa.text(
            "CREATE INDEX ix_events_access_allowed_ids ON events USING GIN (access_allowed_ids);"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_events_code_files_changed ON events USING GIN (code_files_changed);"
        )
    )

    op.execute(sa.text("ALTER TABLE events ENABLE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_events ON events
        USING (tenant_id = current_setting('app.current_tenant')::UUID);
        """)
    )
    op.execute(sa.text("ALTER TABLE events FORCE ROW LEVEL SECURITY;"))

    op.execute(
        sa.text("""
        DO $$
        DECLARE
            week_start DATE;
            week_end DATE;
            part_name TEXT;
        BEGIN
            -- Start from the Monday of the current week
            week_start := date_trunc('week', CURRENT_DATE)::DATE;
            FOR i IN 0..7 LOOP
                week_end := week_start + INTERVAL '7 days';
                part_name := 'events_'
                    || to_char(week_start, 'IYYY')
                    || '_w' || to_char(week_start, 'IW');
                EXECUTE format(
                    'CREATE TABLE IF NOT EXISTS %I'
                    ' PARTITION OF events'
                    ' FOR VALUES FROM (%L) TO (%L)',
                    part_name, week_start, week_end
                );
                week_start := week_end;
            END LOOP;
        END $$;
        """)
    )

    op.create_table(
        "persons",
        sa.Column("person_id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False, index=True),
        sa.Column("display_name", sa.String, nullable=False),
        sa.Column("email", sa.String, nullable=True),
        sa.Column("source_identities", JSONB, nullable=False, server_default="[]"),
        sa.Column("expertise_tags", ARRAY(sa.String), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.execute(sa.text("ALTER TABLE persons ENABLE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_persons ON persons
        USING (tenant_id = current_setting('app.current_tenant')::UUID);
        """)
    )
    op.execute(sa.text("ALTER TABLE persons FORCE ROW LEVEL SECURITY;"))

    op.create_table(
        "agent_ledgers",
        sa.Column("ledger_id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False, index=True),
        sa.Column("agent_session_id", sa.String, nullable=False),
        sa.Column("event_id", UUID, nullable=False),
        sa.Column("action_type", sa.String, nullable=False),
        sa.Column("action_payload", JSONB, nullable=False, server_default="{}"),
        sa.Column("confidence_score", sa.Float, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.execute(sa.text("ALTER TABLE agent_ledgers ENABLE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_agent_ledgers ON agent_ledgers
        USING (tenant_id = current_setting('app.current_tenant')::UUID);
        """)
    )
    op.execute(sa.text("ALTER TABLE agent_ledgers FORCE ROW LEVEL SECURITY;"))

    op.create_table(
        "custom_collections",
        sa.Column("collection_id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False, index=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("event_ids", ARRAY(UUID), nullable=False, server_default="{}"),
        sa.Column("tags", ARRAY(sa.String), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )


def _chat_integration() -> None:
    op.execute(
        sa.text("""
        CREATE TABLE slack_installations (
            id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            team_id             VARCHAR         NOT NULL,
            tenant_id           UUID            NOT NULL,
            bot_token           VARCHAR         NOT NULL,
            installation_info   JSONB           NOT NULL DEFAULT '{}',
            created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
        """)
    )
    op.execute(
        sa.text("""
        CREATE UNIQUE INDEX uq_slack_installations_team_id
        ON slack_installations (team_id);
        """)
    )
    op.execute(
        sa.text("""
        CREATE INDEX ix_slack_installations_tenant_id
        ON slack_installations (tenant_id);
        """)
    )

    op.execute(
        sa.text("""
        CREATE TABLE msteams_installations (
            id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            ms_tenant_id        VARCHAR         NOT NULL,
            tenant_id           UUID            NOT NULL,
            auth_payload        JSONB           NOT NULL DEFAULT '{}',
            created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
        """)
    )
    op.execute(
        sa.text("""
        CREATE UNIQUE INDEX uq_msteams_installations_ms_tenant_id
        ON msteams_installations (ms_tenant_id);
        """)
    )
    op.execute(
        sa.text("""
        CREATE INDEX ix_msteams_installations_tenant_id
        ON msteams_installations (tenant_id);
        """)
    )

    op.execute(
        sa.text("""
        CREATE TABLE chat_users (
            id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           UUID            NOT NULL,
            platform            VARCHAR         NOT NULL,
            native_id           VARCHAR         NOT NULL,
            display_name        VARCHAR         NOT NULL,
            email               VARCHAR,
            metadata            JSONB           NOT NULL DEFAULT '{}',
            created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
        """)
    )
    op.execute(
        sa.text("""
        CREATE UNIQUE INDEX uq_chat_users_tenant_platform_native
        ON chat_users (tenant_id, platform, native_id);
        """)
    )
    op.execute(
        sa.text("""
        CREATE INDEX ix_chat_users_tenant_id
        ON chat_users (tenant_id);
        """)
    )

    op.execute(sa.text("ALTER TABLE chat_users ENABLE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_chat_users ON chat_users
        USING (tenant_id = current_setting('app.current_tenant')::UUID);
        """)
    )
    op.execute(sa.text("ALTER TABLE chat_users FORCE ROW LEVEL SECURITY;"))

    op.execute(
        sa.text("""
        CREATE TABLE chat_channels (
            id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           UUID            NOT NULL,
            platform            VARCHAR         NOT NULL,
            native_id           VARCHAR         NOT NULL,
            display_name        VARCHAR         NOT NULL,
            metadata            JSONB           NOT NULL DEFAULT '{}',
            created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
        """)
    )
    op.execute(
        sa.text("""
        CREATE UNIQUE INDEX uq_chat_channels_tenant_platform_native
        ON chat_channels (tenant_id, platform, native_id);
        """)
    )
    op.execute(
        sa.text("""
        CREATE INDEX ix_chat_channels_tenant_id
        ON chat_channels (tenant_id);
        """)
    )

    op.execute(sa.text("ALTER TABLE chat_channels ENABLE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_chat_channels ON chat_channels
        USING (tenant_id = current_setting('app.current_tenant')::UUID);
        """)
    )
    op.execute(sa.text("ALTER TABLE chat_channels FORCE ROW LEVEL SECURITY;"))

    op.execute(
        sa.text("""
        CREATE TABLE chat_groups (
            id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           UUID            NOT NULL,
            platform            VARCHAR         NOT NULL,
            native_id           VARCHAR         NOT NULL,
            display_name        VARCHAR         NOT NULL,
            metadata            JSONB           NOT NULL DEFAULT '{}',
            created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
        """)
    )
    op.execute(
        sa.text("""
        CREATE UNIQUE INDEX uq_chat_groups_tenant_platform_native
        ON chat_groups (tenant_id, platform, native_id);
        """)
    )
    op.execute(
        sa.text("""
        CREATE INDEX ix_chat_groups_tenant_id
        ON chat_groups (tenant_id);
        """)
    )

    op.execute(sa.text("ALTER TABLE chat_groups ENABLE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_chat_groups ON chat_groups
        USING (tenant_id = current_setting('app.current_tenant')::UUID);
        """)
    )
    op.execute(sa.text("ALTER TABLE chat_groups FORCE ROW LEVEL SECURITY;"))


def _tenant_rls_and_identity() -> None:
    op.execute(
        sa.text("""
        CREATE TABLE enterprise_users (
            id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           UUID            NOT NULL,
            primary_email       VARCHAR         NOT NULL,
            display_name        VARCHAR         NOT NULL,
            active              BOOLEAN         NOT NULL DEFAULT TRUE,
            scim_metadata       JSONB           NOT NULL DEFAULT '{}',
            created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
        """)
    )

    op.execute(
        sa.text("""
        CREATE UNIQUE INDEX uq_enterprise_users_tenant_email
        ON enterprise_users (tenant_id, primary_email);
        """)
    )
    op.execute(
        sa.text("""
        CREATE INDEX ix_enterprise_users_tenant_id
        ON enterprise_users (tenant_id);
        """)
    )
    op.execute(
        sa.text("""
        CREATE INDEX ix_enterprise_users_primary_email
        ON enterprise_users (primary_email);
        """)
    )

    op.execute(
        sa.text("""
        ALTER TABLE chat_users
        ADD COLUMN internal_id UUID REFERENCES enterprise_users(id);
        """)
    )

    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation_events ON events;"))
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation_persons ON persons;"))
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation_agent_ledgers ON agent_ledgers;"))
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation_chat_users ON chat_users;"))
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation_chat_channels ON chat_channels;"))
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation_chat_groups ON chat_groups;"))

    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_events ON events
        USING (tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID);
        """)
    )
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_persons ON persons
        USING (tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID);
        """)
    )
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_agent_ledgers ON agent_ledgers
        USING (tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID);
        """)
    )
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_chat_users ON chat_users
        USING (tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID);
        """)
    )
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_chat_channels ON chat_channels
        USING (tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID);
        """)
    )
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_chat_groups ON chat_groups
        USING (tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID);
        """)
    )

    op.execute(sa.text("ALTER TABLE enterprise_users ENABLE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_enterprise_users ON enterprise_users
        USING (tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID);
        """)
    )
    op.execute(sa.text("ALTER TABLE enterprise_users FORCE ROW LEVEL SECURITY;"))

    op.execute(sa.text("ALTER TABLE custom_collections ENABLE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_custom_collections ON custom_collections
        USING (tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID);
        """)
    )
    op.execute(sa.text("ALTER TABLE custom_collections FORCE ROW LEVEL SECURITY;"))

    op.execute(sa.text("ALTER TABLE slack_installations ENABLE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_slack_installations ON slack_installations
        USING (
            tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID
            OR current_setting('app.bypass_rls', true) = 'true'
        );
        """)
    )
    op.execute(sa.text("ALTER TABLE slack_installations FORCE ROW LEVEL SECURITY;"))

    op.execute(sa.text("ALTER TABLE msteams_installations ENABLE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_msteams_installations ON msteams_installations
        USING (
            tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID
            OR current_setting('app.bypass_rls', true) = 'true'
        );
        """)
    )
    op.execute(sa.text("ALTER TABLE msteams_installations FORCE ROW LEVEL SECURITY;"))


def _github_integration() -> None:
    op.execute(
        sa.text("""
        CREATE TABLE github_installations (
            id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            repo_identifier         VARCHAR         NOT NULL,
            tenant_id               UUID            NOT NULL,
            personal_access_token   VARCHAR         NOT NULL,
            webhook_secret          VARCHAR         NOT NULL,
            created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
        """)
    )

    op.execute(
        sa.text("""
        CREATE UNIQUE INDEX uq_github_installations_repo_identifier
        ON github_installations (repo_identifier);
        """)
    )

    op.execute(
        sa.text("""
        CREATE INDEX ix_github_installations_tenant_id
        ON github_installations (tenant_id);
        """)
    )

    op.execute(sa.text("ALTER TABLE github_installations ENABLE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_github_installations ON github_installations
        USING (
            tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID
            OR current_setting('app.bypass_rls', true) = 'true'
        );
        """)
    )
    op.execute(sa.text("ALTER TABLE github_installations FORCE ROW LEVEL SECURITY;"))


def _jira_integration() -> None:
    op.execute(
        sa.text("""
        CREATE TABLE jira_installations (
            id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            jira_host               VARCHAR         NOT NULL,
            tenant_id               UUID            NOT NULL,
            personal_access_token   VARCHAR         NOT NULL,
            webhook_secret          VARCHAR         NOT NULL,
            created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
        """)
    )

    op.execute(
        sa.text("""
        CREATE UNIQUE INDEX uq_jira_installations_jira_host
        ON jira_installations (jira_host);
        """)
    )

    op.execute(
        sa.text("""
        CREATE INDEX ix_jira_installations_tenant_id
        ON jira_installations (tenant_id);
        """)
    )

    op.execute(sa.text("ALTER TABLE jira_installations ENABLE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_jira_installations ON jira_installations
        USING (
            tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID
            OR current_setting('app.bypass_rls', true) = 'true'
        );
        """)
    )
    op.execute(sa.text("ALTER TABLE jira_installations FORCE ROW LEVEL SECURITY;"))


def _monitoring_integration() -> None:
    op.execute(
        sa.text("""
        CREATE TABLE datadog_installations (
            id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id               UUID            NOT NULL,
            webhook_secret          VARCHAR         NOT NULL,
            created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
        """)
    )

    op.execute(
        sa.text("""
        CREATE INDEX ix_datadog_installations_tenant_id
        ON datadog_installations (tenant_id);
        """)
    )

    op.execute(sa.text("ALTER TABLE datadog_installations ENABLE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_datadog_installations ON datadog_installations
        USING (
            tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID
            OR current_setting('app.bypass_rls', true) = 'true'
        );
        """)
    )
    op.execute(sa.text("ALTER TABLE datadog_installations FORCE ROW LEVEL SECURITY;"))

    op.execute(
        sa.text("""
        CREATE TABLE sentry_installations (
            id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id               UUID            NOT NULL,
            webhook_secret          VARCHAR         NOT NULL,
            created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
        """)
    )

    op.execute(
        sa.text("""
        CREATE INDEX ix_sentry_installations_tenant_id
        ON sentry_installations (tenant_id);
        """)
    )

    op.execute(sa.text("ALTER TABLE sentry_installations ENABLE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_sentry_installations ON sentry_installations
        USING (
            tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID
            OR current_setting('app.bypass_rls', true) = 'true'
        );
        """)
    )
    op.execute(sa.text("ALTER TABLE sentry_installations FORCE ROW LEVEL SECURITY;"))


def _jira_project_keys() -> None:
    op.execute(
        sa.text("""
        CREATE TABLE jira_project_keys (
            id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id               UUID            NOT NULL,
            jira_installation_id    UUID            NOT NULL
                                                    REFERENCES jira_installations(id)
                                                    ON DELETE CASCADE,
            project_key             VARCHAR         NOT NULL,
            created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
        """)
    )

    op.execute(
        sa.text("""
        CREATE UNIQUE INDEX uq_jira_project_keys_tenant_project_key
        ON jira_project_keys (tenant_id, project_key);
        """)
    )

    op.execute(
        sa.text("""
        CREATE INDEX ix_jira_project_keys_tenant_id
        ON jira_project_keys (tenant_id);
        """)
    )

    op.execute(sa.text("ALTER TABLE jira_project_keys ENABLE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_jira_project_keys ON jira_project_keys
        USING (
            tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID
            OR current_setting('app.bypass_rls', true) = 'true'
        );
        """)
    )
    op.execute(sa.text("ALTER TABLE jira_project_keys FORCE ROW LEVEL SECURITY;"))


def _meeting_integration() -> None:
    op.execute(
        sa.text("""
        CREATE TABLE zoom_installations (
            id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id               UUID            NOT NULL,
            zoom_account_id         VARCHAR         NOT NULL UNIQUE,
            webhook_secret          VARCHAR         NOT NULL,
            created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
        """)
    )

    op.execute(
        sa.text("""
        CREATE INDEX ix_zoom_installations_tenant_id
        ON zoom_installations (tenant_id);
        """)
    )

    op.execute(sa.text("ALTER TABLE zoom_installations ENABLE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_zoom_installations ON zoom_installations
        USING (
            tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID
            OR current_setting('app.bypass_rls', true) = 'true'
        );
        """)
    )
    op.execute(sa.text("ALTER TABLE zoom_installations FORCE ROW LEVEL SECURITY;"))

    op.execute(
        sa.text("""
        CREATE TABLE msteams_meeting_installations (
            id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id               UUID            NOT NULL,
            ms_tenant_id            VARCHAR         NOT NULL UNIQUE,
            webhook_secret          VARCHAR         NOT NULL,
            created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
        """)
    )

    op.execute(
        sa.text("""
        CREATE INDEX ix_msteams_meeting_installations_tenant_id
        ON msteams_meeting_installations (tenant_id);
        """)
    )

    op.execute(sa.text("ALTER TABLE msteams_meeting_installations ENABLE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_msteams_meeting_installations
        ON msteams_meeting_installations
        USING (
            tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID
            OR current_setting('app.bypass_rls', true) = 'true'
        );
        """)
    )
    op.execute(sa.text("ALTER TABLE msteams_meeting_installations FORCE ROW LEVEL SECURITY;"))


def _skills_table_and_agent_metadata() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.VARCHAR(), nullable=False),
        sa.Column("description", sa.VARCHAR(), nullable=False),
        sa.Column("instructions", sa.TEXT(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_skills_tenant_id", "skills", ["tenant_id"])

    op.execute(sa.text("ALTER TABLE skills ENABLE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_skills ON skills
        USING (tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID);
        """)
    )
    op.execute(sa.text("ALTER TABLE skills FORCE ROW LEVEL SECURITY;"))

    op.add_column("events", sa.Column("agent_metadata", postgresql.JSONB(), nullable=True))


def _modify_agent_ledgers_table() -> None:
    op.alter_column("agent_ledgers", "ledger_id", new_column_name="id")

    op.alter_column("agent_ledgers", "agent_session_id", nullable=True)
    op.alter_column("agent_ledgers", "event_id", nullable=True)

    op.drop_column("agent_ledgers", "action_type")
    op.drop_column("agent_ledgers", "action_payload")
    op.drop_column("agent_ledgers", "confidence_score")

    op.add_column("agent_ledgers", sa.Column("scope_type", sa.VARCHAR(), nullable=False))
    op.add_column("agent_ledgers", sa.Column("scope_id", sa.VARCHAR(), nullable=False))
    op.add_column("agent_ledgers", sa.Column("key", sa.VARCHAR(), nullable=False))
    op.add_column(
        "agent_ledgers", sa.Column("value", postgresql.JSONB(), server_default="{}", nullable=False)
    )
    op.add_column(
        "agent_ledgers",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_unique_constraint(
        "uq_agent_ledgers_scope_key",
        "agent_ledgers",
        ["tenant_id", "scope_type", "scope_id", "key"],
    )
    op.create_index(
        "ix_agent_ledgers_scope",
        "agent_ledgers",
        ["tenant_id", "scope_type", "scope_id"],
    )


def _canonical_contexts() -> None:
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector;"))

    op.create_table(
        "canonical_contexts",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("confidence_score", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_canonical_contexts_tenant_id", "canonical_contexts", ["tenant_id"])

    op.create_table(
        "context_event_links",
        sa.Column("canonical_id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["canonical_id"], ["canonical_contexts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("canonical_id", "event_id"),
    )
    op.create_index("ix_context_event_links_tenant_id", "context_event_links", ["tenant_id"])

    op.execute(sa.text("ALTER TABLE canonical_contexts ENABLE ROW LEVEL SECURITY;"))
    op.execute(sa.text("ALTER TABLE canonical_contexts FORCE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_canonical ON canonical_contexts
        USING (tenant_id = current_setting('app.current_tenant')::UUID);
        """)
    )

    op.execute(sa.text("ALTER TABLE context_event_links ENABLE ROW LEVEL SECURITY;"))
    op.execute(sa.text("ALTER TABLE context_event_links FORCE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_context_links ON context_event_links
        USING (tenant_id = current_setting('app.current_tenant')::UUID);
        """)
    )


def _ast_nodes_schema() -> None:
    op.execute(sa.text("CREATE TYPE ast_edge_status AS ENUM ('active', 'stale');"))

    op.create_table(
        "ast_nodes",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("repo_identifier", sa.String(), nullable=False),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column("symbol_name", sa.String(), nullable=False),
        sa.Column("node_type", sa.String(), nullable=False),
        sa.Column("merkle_hash", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "repo_identifier",
            "file_path",
            "symbol_name",
            "node_type",
            name="uq_ast_nodes_identifier",
        ),
    )
    op.create_index(op.f("ix_ast_nodes_tenant_id"), "ast_nodes", ["tenant_id"], unique=False)

    op.create_table(
        "ast_context_edges",
        sa.Column("ast_node_id", sa.UUID(), nullable=False),
        sa.Column("canonical_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM("active", "stale", name="ast_edge_status", create_type=False),
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["ast_node_id"], ["ast_nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["canonical_id"], ["canonical_contexts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("ast_node_id", "canonical_id"),
    )
    op.create_index(
        op.f("ix_ast_context_edges_tenant_id"), "ast_context_edges", ["tenant_id"], unique=False
    )

    op.execute(sa.text("ALTER TABLE ast_nodes ENABLE ROW LEVEL SECURITY;"))
    op.execute(sa.text("ALTER TABLE ast_nodes FORCE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_ast_nodes ON ast_nodes
        USING (tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID);
        """)
    )

    op.execute(sa.text("ALTER TABLE ast_context_edges ENABLE ROW LEVEL SECURITY;"))
    op.execute(sa.text("ALTER TABLE ast_context_edges FORCE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_ast_context_edges ON ast_context_edges
        USING (tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID);
        """)
    )


def _last_accessed_to_ast_context_edges() -> None:
    op.add_column(
        "ast_context_edges",
        sa.Column(
            "last_accessed",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def _is_locked_to_canonical_contexts() -> None:
    op.add_column(
        "canonical_contexts",
        sa.Column(
            "is_locked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def _deprecated_to_ast_edge_status() -> None:
    op.execute(sa.text("ALTER TYPE ast_edge_status ADD VALUE 'deprecated'"))


def _enriched_metadata_columns_to_events() -> None:
    op.add_column(
        "events",
        sa.Column(
            "incident_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "events",
        sa.Column(
            "meeting_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "events",
        sa.Column(
            "chat_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def _meeting_extraction_tables() -> None:
    op.execute(
        sa.text("""
        CREATE TABLE meeting_discourse_segments (
            segment_id          UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            parent_event_id     UUID            NOT NULL,
            tenant_id           UUID            NOT NULL,
            start_time          DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            end_time            DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            topics              JSONB           NOT NULL DEFAULT '[]',
            speakers            JSONB           NOT NULL DEFAULT '[]',
            text_content        TEXT            NOT NULL,
            confidence_score    DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
        """)
    )

    op.execute(
        sa.text("""
        CREATE TABLE extracted_adrs (
            id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            segment_id          UUID            NOT NULL
                REFERENCES meeting_discourse_segments(segment_id) ON DELETE CASCADE,
            tenant_id           UUID            NOT NULL,
            decision_title      VARCHAR         NOT NULL,
            context             TEXT            NOT NULL,
            alternatives_considered JSONB       NOT NULL DEFAULT '[]',
            final_decision      TEXT            NOT NULL,
            primary_driver      VARCHAR,
            created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
        """)
    )

    op.execute(
        sa.text("""
        CREATE TABLE extracted_constraints (
            id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            segment_id          UUID            NOT NULL
                REFERENCES meeting_discourse_segments(segment_id) ON DELETE CASCADE,
            tenant_id           UUID            NOT NULL,
            constraint_type     VARCHAR         NOT NULL,
            description         TEXT            NOT NULL,
            source_quote        TEXT            NOT NULL,
            created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
        """)
    )

    op.execute(
        sa.text("""
        CREATE TABLE extracted_risks (
            id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            segment_id          UUID            NOT NULL
                REFERENCES meeting_discourse_segments(segment_id) ON DELETE CASCADE,
            tenant_id           UUID            NOT NULL,
            risk_level          VARCHAR         NOT NULL,
            description         TEXT            NOT NULL,
            potential_mitigation TEXT,
            deferred_action_items JSONB         NOT NULL DEFAULT '[]',
            created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
        """)
    )

    op.execute(
        sa.text("CREATE INDEX ix_meeting_segments_tenant ON meeting_discourse_segments(tenant_id);")
    )
    op.execute(sa.text("CREATE INDEX ix_extracted_adrs_tenant ON extracted_adrs(tenant_id);"))
    op.execute(
        sa.text("CREATE INDEX ix_extracted_constraints_tenant ON extracted_constraints(tenant_id);")
    )
    op.execute(sa.text("CREATE INDEX ix_extracted_risks_tenant ON extracted_risks(tenant_id);"))

    tables = [
        "meeting_discourse_segments",
        "extracted_adrs",
        "extracted_constraints",
        "extracted_risks",
    ]

    for table in tables:
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;"))
        op.execute(
            sa.text(f"""
            CREATE POLICY tenant_isolation_{table} ON {table}
            USING (tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID);
            """)
        )
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;"))


def _audit_status_to_extractions() -> None:
    tables = ["extracted_adrs", "extracted_constraints", "extracted_risks"]

    for table in tables:
        op.execute(
            sa.text(
                f"ALTER TABLE {table} "
                "ADD COLUMN audit_status VARCHAR NOT NULL DEFAULT 'pending_audit';"
            )
        )
        op.execute(
            sa.text(f"CREATE INDEX ix_{table}_audit_status ON {table}(tenant_id, audit_status);")
        )


def _api_keys_and_installations() -> None:
    op.execute(
        sa.text("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            hashed_token        VARCHAR         NOT NULL,
            tenant_id           UUID            NOT NULL,
            name                VARCHAR         NOT NULL DEFAULT 'Default API Key',
            active              BOOLEAN         NOT NULL DEFAULT TRUE,
            created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
        """)
    )

    op.execute(
        sa.text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_api_keys_hashed_token
        ON api_keys (hashed_token);
        """)
    )
    op.execute(
        sa.text("""
        CREATE INDEX IF NOT EXISTS ix_api_keys_tenant_id
        ON api_keys (tenant_id);
        """)
    )

    op.execute(
        sa.text("""
        CREATE TABLE IF NOT EXISTS notion_installations (
            id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            notion_workspace_id     VARCHAR         NOT NULL,
            tenant_id               UUID            NOT NULL,
            access_token            VARCHAR         NOT NULL,
            webhook_secret          VARCHAR         NOT NULL DEFAULT '',
            created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
        """)
    )

    op.execute(
        sa.text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_notion_installations_workspace_id
        ON notion_installations (notion_workspace_id);
        """)
    )
    op.execute(
        sa.text("""
        CREATE INDEX IF NOT EXISTS ix_notion_installations_tenant_id
        ON notion_installations (tenant_id);
        """)
    )

    op.execute(
        sa.text("""
        CREATE TABLE IF NOT EXISTS linear_installations (
            id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            linear_organization_id  VARCHAR         NOT NULL,
            tenant_id               UUID            NOT NULL,
            access_token            VARCHAR         NOT NULL,
            webhook_secret          VARCHAR         NOT NULL DEFAULT '',
            created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
        """)
    )

    op.execute(
        sa.text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_linear_installations_org_id
        ON linear_installations (linear_organization_id);
        """)
    )
    op.execute(
        sa.text("""
        CREATE INDEX IF NOT EXISTS ix_linear_installations_tenant_id
        ON linear_installations (tenant_id);
        """)
    )

    tables = ["api_keys", "notion_installations", "linear_installations"]
    for table in tables:
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;"))
        op.execute(
            sa.text(f"""
            CREATE POLICY tenant_isolation_{table} ON {table}
            USING (
                tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID
                OR current_setting('app.bypass_rls', true) = 'true'
            );
            """)
        )
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;"))


def _jira_installs_updated_at() -> None:
    op.execute(
        sa.text("""
        ALTER TABLE jira_installations
        ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
        """)
    )


def _access_control_to_canonical_contexts() -> None:
    op.add_column(
        "canonical_contexts",
        sa.Column(
            "access_control",
            JSONB,
            nullable=False,
            server_default=_DEFAULT_ACCESS_CONTROL_JSON,
        ),
    )

    conn = op.get_bind()
    context_ids = [row[0] for row in conn.execute(sa.text("SELECT id FROM canonical_contexts"))]

    for context_id in context_ids:
        event_rows = conn.execute(
            sa.text(
                "SELECT e.access_control, e.access_allowed_ids "
                "FROM context_event_links cel "
                "JOIN events e ON e.event_id = cel.event_id AND e.tenant_id = cel.tenant_id "
                "WHERE cel.canonical_id = :cid"
            ),
            {"cid": context_id},
        ).fetchall()

        if not event_rows:
            continue

        is_public = False
        admin_overrides: set[str] = set()
        allowed_sets: list[set[str]] = []

        for ac_raw, allowed_ids_raw in event_rows:
            ac = json.loads(ac_raw) if isinstance(ac_raw, str) else (ac_raw or {})
            if ac.get("is_public", False):
                is_public = True
            admin_overrides.update(str(uid) for uid in (ac.get("admin_overrides") or []))
            allowed_sets.append({str(uid) for uid in (allowed_ids_raw or [])})

        if is_public:
            allowed_identity_ids: list[str] = []
        elif allowed_sets:
            allowed_identity_ids = sorted(set.intersection(*allowed_sets))
        else:
            allowed_identity_ids = []

        new_access_control = {
            "is_public": is_public,
            "visibility": "public" if is_public else "restricted",
            "acl_type": "open",
            "allowed_identity_ids": allowed_identity_ids,
            "source_group_id": None,
            "inherited": False,
            "admin_overrides": sorted(admin_overrides),
        }

        conn.execute(
            sa.text("UPDATE canonical_contexts SET access_control = :ac WHERE id = :cid"),
            {"ac": json.dumps(new_access_control), "cid": context_id},
        )


def _per_user_access_control_rls() -> None:
    op.execute(
        sa.text("""
        CREATE POLICY user_access_control_canonical ON canonical_contexts
        AS RESTRICTIVE
        USING (
            current_setting('app.current_user_id', true) IS NULL
            OR COALESCE((access_control->>'is_public')::boolean, false)
            OR (access_control->'allowed_identity_ids')
                ? current_setting('app.current_user_id', true)
            OR (access_control->'admin_overrides')
                ? current_setting('app.current_user_id', true)
        );
        """)
    )

    op.execute(
        sa.text("""
        CREATE POLICY user_access_control_events ON events
        AS RESTRICTIVE
        USING (
            current_setting('app.current_user_id', true) IS NULL
            OR COALESCE((access_control->>'is_public')::boolean, false)
            OR current_setting('app.current_user_id', true)::uuid = ANY(access_allowed_ids)
            OR (access_control->'admin_overrides')
                ? current_setting('app.current_user_id', true)
        );
        """)
    )


def _scope_repo_identifier_to_canonical_contexts() -> None:
    op.add_column(
        "canonical_contexts",
        sa.Column("scope_repo_identifier", sa.String(), nullable=True),
    )

    conn = op.get_bind()
    context_ids = [row[0] for row in conn.execute(sa.text("SELECT id FROM canonical_contexts"))]

    for context_id in context_ids:
        edge_rows = conn.execute(
            sa.text(
                "SELECT n.repo_identifier "
                "FROM ast_context_edges e "
                "JOIN ast_nodes n ON n.id = e.ast_node_id AND n.tenant_id = e.tenant_id "
                "WHERE e.canonical_id = :cid AND e.status = 'active' "
                "ORDER BY e.created_at DESC"
            ),
            {"cid": context_id},
        ).fetchall()

        if not edge_rows:
            continue

        distinct_repos = {row[0] for row in edge_rows}
        if len(distinct_repos) > 1:
            logger.warning(
                "canonical_context %s has active AST links to %d distinct repos (%s); "
                "arbitrarily scoping to the most-recently-linked one: %s",
                context_id,
                len(distinct_repos),
                sorted(distinct_repos),
                edge_rows[0][0],
            )

        most_recent_repo_identifier = edge_rows[0][0]
        conn.execute(
            sa.text("UPDATE canonical_contexts SET scope_repo_identifier = :repo WHERE id = :cid"),
            {"repo": most_recent_repo_identifier, "cid": context_id},
        )


def _context_scope_audit_log() -> None:
    op.create_table(
        "context_scope_audit_log",
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column(
            "canonical_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("canonical_contexts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("changed_by", sa.String(), nullable=False),
        sa.Column("previous_scope_repo_identifier", sa.String(), nullable=True),
        sa.Column("new_scope_repo_identifier", sa.String(), nullable=True),
        sa.Column("previous_is_locked", sa.Boolean(), nullable=False),
        sa.Column("new_is_locked", sa.Boolean(), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_context_scope_audit_log_tenant_id",
        "context_scope_audit_log",
        ["tenant_id"],
    )
    op.create_index(
        "ix_context_scope_audit_log_canonical_id",
        "context_scope_audit_log",
        ["canonical_id"],
    )

    op.execute(sa.text("ALTER TABLE context_scope_audit_log ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE context_scope_audit_log FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_context_scope_audit_log ON context_scope_audit_log
        USING (tenant_id = current_setting('app.current_tenant')::UUID);
        """)
    )


def _content_hash_and_last_verified_at_to_canonical_contexts() -> None:
    op.add_column(
        "canonical_contexts",
        sa.Column("content_hash", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "canonical_contexts",
        sa.Column(
            "last_verified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, summary FROM canonical_contexts")).fetchall()
    for context_id, summary in rows:
        digest = hashlib.sha256((summary or "").encode("utf-8")).hexdigest()
        conn.execute(
            sa.text("UPDATE canonical_contexts SET content_hash = :h WHERE id = :cid"),
            {"h": digest, "cid": context_id},
        )

    conn.execute(sa.text("UPDATE canonical_contexts SET last_verified_at = created_at"))


def _audit_log_entries_and_api_key_is_admin() -> None:
    op.add_column(
        "api_keys",
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default="false"),
    )

    op.create_table(
        "audit_log_entries",
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("record_type", sa.String(), nullable=True),
        sa.Column("record_id", sa.String(), nullable=True),
        sa.Column("actor", sa.String(), nullable=True),
        sa.Column("event_metadata", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_audit_log_entries_tenant_id", "audit_log_entries", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_audit_log_entries_event_type", "audit_log_entries", ["event_type"], unique=False
    )
    op.create_index(
        "ix_audit_log_entries_created_at", "audit_log_entries", ["created_at"], unique=False
    )


def _severity_to_canonical_contexts() -> None:
    op.add_column(
        "canonical_contexts",
        sa.Column("severity", sa.String(), nullable=False, server_default="soft_warn"),
    )


def _summary_tsv_to_canonical_contexts() -> None:
    op.add_column(
        "canonical_contexts",
        sa.Column(
            "summary_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', summary)", persisted=True),
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_canonical_contexts_summary_tsv "
            "ON canonical_contexts USING gin (summary_tsv);"
        )
    )


def _context_repo_scopes() -> None:
    op.create_table(
        "context_repo_scopes",
        sa.Column("canonical_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("repo_identifier", sa.String(), nullable=False),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["canonical_id"], ["canonical_contexts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("canonical_id", "repo_identifier"),
    )
    op.create_index("ix_context_repo_scopes_tenant_id", "context_repo_scopes", ["tenant_id"])

    op.execute(sa.text("ALTER TABLE context_repo_scopes ENABLE ROW LEVEL SECURITY;"))
    op.execute(sa.text("ALTER TABLE context_repo_scopes FORCE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_context_repo_scopes ON context_repo_scopes
        USING (tenant_id = current_setting('app.current_tenant')::UUID);
        """)
    )

    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO context_repo_scopes "
            "(canonical_id, repo_identifier, tenant_id, created_at) "
            "SELECT id, scope_repo_identifier, tenant_id, now() "
            "FROM canonical_contexts WHERE scope_repo_identifier IS NOT NULL"
        )
    )

    op.alter_column(
        "context_scope_audit_log",
        "previous_scope_repo_identifier",
        new_column_name="previous_scope_repo_identifiers",
    )
    op.alter_column(
        "context_scope_audit_log",
        "new_scope_repo_identifier",
        new_column_name="new_scope_repo_identifiers",
    )
    op.execute(
        sa.text(
            "ALTER TABLE context_scope_audit_log "
            "ALTER COLUMN previous_scope_repo_identifiers TYPE JSONB "
            "USING (CASE WHEN previous_scope_repo_identifiers IS NULL THEN NULL "
            "ELSE to_jsonb(ARRAY[previous_scope_repo_identifiers]) END)"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE context_scope_audit_log "
            "ALTER COLUMN new_scope_repo_identifiers TYPE JSONB "
            "USING (CASE WHEN new_scope_repo_identifiers IS NULL THEN NULL "
            "ELSE to_jsonb(ARRAY[new_scope_repo_identifiers]) END)"
        )
    )

    op.drop_column("canonical_contexts", "scope_repo_identifier")


def _embedding_hnsw_index() -> None:
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_canonical_contexts_embedding_hnsw "
            "ON canonical_contexts USING hnsw (embedding vector_cosine_ops);"
        )
    )


def _workflow_definitions() -> None:
    op.create_table(
        "workflow_definitions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("repo_identifier", sa.String(), nullable=False),
        sa.Column("workflow_path", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column(
            "jobs",
            postgresql.JSONB(),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("commit_sha", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "repo_identifier",
            "workflow_path",
            name="uq_workflow_definitions_identifier",
        ),
    )
    op.create_index(
        op.f("ix_workflow_definitions_tenant_id"),
        "workflow_definitions",
        ["tenant_id"],
        unique=False,
    )

    op.execute(sa.text("ALTER TABLE workflow_definitions ENABLE ROW LEVEL SECURITY;"))
    op.execute(sa.text("ALTER TABLE workflow_definitions FORCE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_workflow_definitions ON workflow_definitions
        USING (tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID);
        """)
    )


def _ci_failure_context_links() -> None:
    op.create_table(
        "ci_failure_context_links",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("ci_event_id", sa.UUID(), nullable=False),
        sa.Column("canonical_id", sa.UUID(), nullable=False),
        sa.Column("ast_node_id", sa.UUID(), nullable=True),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["canonical_id"], ["canonical_contexts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ast_node_id"], ["ast_nodes.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "ci_event_id",
            "canonical_id",
            "file_path",
            name="uq_ci_failure_context_links_identifier",
        ),
    )
    op.create_index(
        op.f("ix_ci_failure_context_links_tenant_id"),
        "ci_failure_context_links",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ci_failure_context_links_ci_event_id"),
        "ci_failure_context_links",
        ["ci_event_id"],
        unique=False,
    )

    op.execute(sa.text("ALTER TABLE ci_failure_context_links ENABLE ROW LEVEL SECURITY;"))
    op.execute(sa.text("ALTER TABLE ci_failure_context_links FORCE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_ci_failure_context_links ON ci_failure_context_links
        USING (tenant_id = nullif(current_setting('app.current_tenant', true), '')::UUID);
        """)
    )


def _generalize_context_repo_scopes() -> None:
    op.alter_column("context_repo_scopes", "repo_identifier", new_column_name="scope_value")
    op.add_column(
        "context_repo_scopes",
        sa.Column("scope_type", sa.String(), nullable=False, server_default="repo"),
    )
    op.add_column(
        "context_repo_scopes",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="2"),
    )

    op.drop_constraint("context_repo_scopes_pkey", "context_repo_scopes", type_="primary")
    op.create_primary_key(
        "context_repo_scopes_pkey",
        "context_repo_scopes",
        ["canonical_id", "scope_type", "scope_value"],
    )

    op.create_index("ix_context_repo_scopes_scope_type", "context_repo_scopes", ["scope_type"])


def _context_type_to_canonical_contexts() -> None:
    op.add_column(
        "canonical_contexts",
        sa.Column("context_type", sa.String(), nullable=False, server_default="general"),
    )


def _user_repo_affinity() -> None:
    op.create_table(
        "user_repo_affinity",
        sa.Column(
            "id", PG_UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("repo_identifier", sa.String(), nullable=False),
        sa.Column("query_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "last_queried_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "repo_identifier", name="uq_user_repo_affinity_identifier"
        ),
    )
    op.create_index("ix_user_repo_affinity_tenant_id", "user_repo_affinity", ["tenant_id"])
    op.create_index(
        "ix_user_repo_affinity_tenant_user", "user_repo_affinity", ["tenant_id", "user_id"]
    )

    op.execute(sa.text("ALTER TABLE user_repo_affinity ENABLE ROW LEVEL SECURITY;"))
    op.execute(sa.text("ALTER TABLE user_repo_affinity FORCE ROW LEVEL SECURITY;"))
    op.execute(
        sa.text("""
        CREATE POLICY tenant_isolation_user_repo_affinity ON user_repo_affinity
        USING (tenant_id = current_setting('app.current_tenant')::UUID);
        """)
    )
    op.execute(
        sa.text("""
        CREATE POLICY user_access_control_user_repo_affinity ON user_repo_affinity
        AS RESTRICTIVE
        USING (
            current_setting('app.current_user_id', true) IS NULL
            OR user_id = current_setting('app.current_user_id', true)::UUID
        );
        """)
    )


def _typed_scope_rows_to_audit_log() -> None:
    op.add_column(
        "context_scope_audit_log", sa.Column("previous_scope_rows", JSONB(), nullable=True)
    )
    op.add_column("context_scope_audit_log", sa.Column("new_scope_rows", JSONB(), nullable=True))


def _events_default_partition() -> None:
    op.execute(sa.text("CREATE TABLE IF NOT EXISTS events_default PARTITION OF events DEFAULT;"))


def _app_role_support() -> None:
    for table, policy in _TENANT_POLICIES.items():
        op.execute(sa.text(f"ALTER POLICY {policy} ON {table} USING (tenant_id = {_TENANT})"))
    for (table, policy), expr in _USER_POLICIES_NEW.items():
        op.execute(sa.text(f"ALTER POLICY {policy} ON {table} USING ({expr})"))

    op.execute(sa.text(_ENSURE_PARTITION))
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION checkcord_ensure_events_partition(text, date, date) FROM PUBLIC"
        )
    )
    op.execute(sa.text(_PARTITIONS_RLS.replace("{action}", "ENABLE")))


def _api_key_user_binding() -> None:
    op.create_unique_constraint(
        "uq_enterprise_users_id_tenant", "enterprise_users", ["id", "tenant_id"]
    )
    op.add_column("api_keys", sa.Column("user_id", PG_UUID(as_uuid=True), nullable=True))
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    op.create_foreign_key(
        "fk_api_keys_user_tenant",
        "api_keys",
        "enterprise_users",
        ["user_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="CASCADE",
    )


def _pipeline_state_to_events() -> None:
    op.add_column(
        "events",
        sa.Column("pipeline_state", sa.String(), nullable=False, server_default="done"),
    )
    op.add_column(
        "events",
        sa.Column("pipeline_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "events", sa.Column("pipeline_started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "events", sa.Column("pipeline_next_attempt_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("events", sa.Column("pipeline_last_error", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_events_pipeline_state",
        "events",
        "pipeline_state IN ('pending', 'done', 'failed')",
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_events_pipeline_unfinished ON events "
            "(tenant_id, pipeline_state, pipeline_next_attempt_at) "
            "WHERE pipeline_state <> 'done'"
        )
    )


def _audit_log_reads_and_admin_actions() -> None:
    op.execute(sa.text("ALTER TABLE audit_log_entries RENAME COLUMN event_type TO action"))
    op.execute(sa.text("ALTER TABLE audit_log_entries RENAME COLUMN event_metadata TO params"))
    op.execute(
        sa.text("""
        ALTER TABLE audit_log_entries
            ADD COLUMN channel VARCHAR NOT NULL DEFAULT 'api',
            ADD COLUMN user_id UUID,
            ADD COLUMN user_email VARCHAR,
            ADD COLUMN key_id UUID,
            ADD COLUMN key_name VARCHAR,
            ADD COLUMN record_ids TEXT[] NOT NULL DEFAULT '{}',
            ADD COLUMN redacted_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN result_count INTEGER
        """)
    )
    op.execute(
        sa.text("""
        UPDATE audit_log_entries SET
            record_ids = CASE WHEN record_id IS NULL THEN '{}' ELSE ARRAY[record_id] END,
            redacted_count = CASE WHEN action = 'redaction_applied' THEN 1 ELSE 0 END,
            params = params || jsonb_strip_nulls(
                jsonb_build_object('record_type', record_type, 'actor', actor)
            )
        """)
    )
    op.execute(
        sa.text("""
        ALTER TABLE audit_log_entries
            ALTER COLUMN channel DROP DEFAULT,
            DROP COLUMN record_type,
            DROP COLUMN record_id,
            DROP COLUMN actor,
            ADD CONSTRAINT ck_audit_log_entries_channel
                CHECK (channel IN ('mcp', 'api', 'cli', 'scim'))
        """)
    )
    for name in _OLD_INDEXES:
        op.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
    for name, spec in _NEW_INDEXES.items():
        op.execute(sa.text(f"CREATE INDEX {name} ON audit_log_entries {spec}"))

    op.execute(sa.text("ALTER TABLE audit_log_entries ENABLE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY audit_log_entries_select ON audit_log_entries "
            f"FOR SELECT USING ({_TENANT_OR_BYPASS})"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY audit_log_entries_insert ON audit_log_entries "
            f"FOR INSERT WITH CHECK ({_TENANT_OR_BYPASS})"
        )
    )
    op.execute(sa.text("ALTER TABLE audit_log_entries FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            _if_app_role("REVOKE UPDATE, DELETE, TRUNCATE ON audit_log_entries FROM checkcord_app;")
        )
    )


def _audience_containers() -> None:
    op.create_table(
        "audience_containers",
        sa.Column(
            "id", PG_UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("container", sa.String(), nullable=False),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("member_ids", ARRAY(PG_UUID(as_uuid=True)), nullable=False, server_default="{}"),
        sa.Column("details", JSONB(), nullable=False, server_default="{}"),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "platform", "container", name="uq_audience_containers_container"
        ),
    )
    op.execute(sa.text("ALTER TABLE audience_containers ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE audience_containers FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation_audience_containers ON audience_containers "
            f"USING ({_TENANT_OR_BYPASS}) WITH CHECK ({_TENANT_OR_BYPASS})"
        )
    )

    op.add_column("events", sa.Column("audience_container", sa.String(), nullable=True))
    op.execute(
        sa.text(
            "CREATE INDEX ix_events_audience_container ON events "
            "(tenant_id, source_platform, audience_container) "
            "WHERE audience_container IS NOT NULL"
        )
    )
    op.execute(sa.text(_BACKFILL))
    op.execute(sa.text(_SEED))


def _event_deletion() -> None:
    op.drop_column("events", "retention_class")
    op.add_column("events", sa.Column("connection", sa.String(), nullable=True))
    op.execute(
        sa.text(
            "CREATE INDEX ix_events_connection ON events (tenant_id, connection) "
            "WHERE connection IS NOT NULL"
        )
    )

    op.add_column(
        "canonical_contexts",
        sa.Column("origin_event_id", PG_UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_canonical_contexts_origin_event_id ON canonical_contexts "
            "(origin_event_id) WHERE origin_event_id IS NOT NULL"
        )
    )
    op.execute(sa.text(_BACKFILL_ORIGIN))

    op.create_index("ix_context_event_links_event_id", "context_event_links", ["event_id"])
    op.create_index(
        "ix_meeting_segments_parent_event_id", "meeting_discourse_segments", ["parent_event_id"]
    )


def _connection_review() -> None:
    op.create_table(
        "connection_reviews",
        sa.Column("tenant_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("connection", sa.String(), nullable=False),
        sa.Column("in_review", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", "connection"),
    )
    op.execute(sa.text("ALTER TABLE connection_reviews ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE connection_reviews FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation_connection_reviews ON connection_reviews "
            f"USING ({_TENANT_OR_BYPASS}) WITH CHECK ({_TENANT_OR_BYPASS})"
        )
    )
    op.execute(sa.text(_SEED_HELD))

    op.add_column("events", sa.Column("held_connection", sa.String(), nullable=True))
    op.execute(
        sa.text(
            "CREATE INDEX ix_events_held_connection ON events (tenant_id, held_connection) "
            "WHERE held_connection IS NOT NULL"
        )
    )
    op.add_column("canonical_contexts", sa.Column("held_connection", sa.String(), nullable=True))
    op.execute(
        sa.text(
            "CREATE INDEX ix_canonical_contexts_held_connection ON canonical_contexts "
            "(tenant_id, held_connection) WHERE held_connection IS NOT NULL"
        )
    )
    op.add_column("meeting_discourse_segments", sa.Column("connection", sa.String(), nullable=True))

    for table, name in (
        ("events", "review_hold_events"),
        ("canonical_contexts", "review_hold_canonical"),
    ):
        op.execute(
            sa.text(f"CREATE POLICY {name} ON {table} AS RESTRICTIVE FOR ALL USING ({_HELD})")
        )

    op.execute(sa.text(_CHECK_LINKS))
    for name, table, timing, when, kind in _TRIGGERS:
        op.execute(
            sa.text(
                f"CREATE CONSTRAINT TRIGGER {name} {timing} ON {table} "
                f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW {when}"
                f"EXECUTE FUNCTION checkcord_check_held_links('{kind}')"
            )
        )


def _stored_redaction_and_model_usage() -> None:
    op.add_column("canonical_contexts", sa.Column("summary_redacted", sa.Text(), nullable=True))
    op.add_column("canonical_contexts", sa.Column("redaction_version", sa.String(), nullable=True))
    op.add_column(
        "canonical_contexts",
        sa.Column("redaction_pending", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_canonical_contexts_redaction_pending ON canonical_contexts "
            "(tenant_id, id) WHERE redaction_pending"
        )
    )

    op.add_column("meeting_discourse_segments", sa.Column("author", JSONB(), nullable=True))
    op.add_column("meeting_discourse_segments", sa.Column("access_control", JSONB(), nullable=True))

    op.create_table(
        "model_usage",
        sa.Column("tenant_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("calls", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("failures", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("tenant_id", "day", "kind", "purpose", "model"),
    )
    op.execute(sa.text("ALTER TABLE model_usage ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE model_usage FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation_model_usage ON model_usage "
            f"USING ({_TENANT_OR_BYPASS}) WITH CHECK ({_TENANT_OR_BYPASS})"
        )
    )
    op.execute(
        sa.text(
            _if_app_role("GRANT SELECT, INSERT, UPDATE, DELETE ON model_usage TO checkcord_app;")
        )
    )


def _sign_in_sessions() -> None:
    op.add_column(
        "enterprise_users",
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_enterprise_users_tenant_lower_email ON enterprise_users "
            "(tenant_id, lower(primary_email))"
        )
    )

    op.create_table(
        "user_sessions",
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("hashed_token", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.UniqueConstraint("hashed_token", name="uq_user_sessions_hashed_token"),
        sa.ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["enterprise_users.id", "enterprise_users.tenant_id"],
            name="fk_user_sessions_user_tenant",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_user_sessions_tenant_user", "user_sessions", ["tenant_id", "user_id"])
    op.execute(sa.text("ALTER TABLE user_sessions ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE user_sessions FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation_user_sessions ON user_sessions "
            f"USING ({_TENANT_OR_BYPASS}) WITH CHECK ({_TENANT_OR_BYPASS})"
        )
    )
    op.execute(
        sa.text(
            _if_app_role("GRANT SELECT, INSERT, UPDATE, DELETE ON user_sessions TO checkcord_app;")
        )
    )


def _scheduled_jobs_and_teams_subscription() -> None:
    op.create_table(
        "scheduled_jobs",
        sa.Column("job_id", sa.String(), primary_key=True),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_succeeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(sa.text(_if_app_role("REVOKE ALL ON scheduled_jobs FROM checkcord_app;")))
    op.execute(
        sa.text(_if_app_role("GRANT SELECT, INSERT, UPDATE ON scheduled_jobs TO checkcord_app;"))
    )

    op.add_column(
        "msteams_meeting_installations",
        sa.Column("subscription_id", sa.String(), nullable=True),
    )
    op.add_column(
        "msteams_meeting_installations",
        sa.Column("subscription_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "msteams_meeting_installations",
        sa.Column("subscription_error", sa.String(), nullable=True),
    )


def _record_lifecycle() -> None:
    op.add_column(
        "canonical_contexts", sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("canonical_contexts", sa.Column("retired_by", sa.String(), nullable=True))
    op.add_column("canonical_contexts", sa.Column("retired_reason", sa.Text(), nullable=True))
    op.add_column(
        "canonical_contexts",
        sa.Column(
            "replaced_by",
            PG_UUID(as_uuid=True),
            sa.ForeignKey(
                "canonical_contexts.id",
                name="fk_canonical_contexts_replaced_by",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_canonical_contexts_retired ON canonical_contexts "
            "(tenant_id, retired_at) WHERE retired_at IS NOT NULL"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY retired_canonical ON canonical_contexts AS RESTRICTIVE "
            f"FOR ALL USING ({_RETIRED})"
        )
    )

    op.create_table(
        "record_replacement_suggestions",
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column(
            "record_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("canonical_contexts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "replacement_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("canonical_contexts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("basis", sa.String(), nullable=False),
        sa.Column("similarity", sa.Float(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_by", sa.String(), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "record_id", "replacement_id", name="uq_record_replacement_suggestions"
        ),
        sa.CheckConstraint("basis IN ('stated', 'model')", name="ck_replacement_basis"),
        sa.CheckConstraint("record_id <> replacement_id", name="ck_replacement_not_self"),
    )
    op.create_index(
        "ix_record_replacement_suggestions_replacement",
        "record_replacement_suggestions",
        ["replacement_id"],
    )
    op.execute(sa.text("ALTER TABLE record_replacement_suggestions ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE record_replacement_suggestions FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation_record_replacement_suggestions "
            f"ON record_replacement_suggestions USING ({_TENANT_OR_BYPASS}) "
            f"WITH CHECK ({_TENANT_OR_BYPASS})"
        )
    )
    op.execute(
        sa.text(
            _if_app_role(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON record_replacement_suggestions "
                "TO checkcord_app;"
            )
        )
    )


def _connection_scope() -> None:
    op.create_table(
        "connection_scopes",
        sa.Column("tenant_id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("connection", sa.String(), primary_key=True),
        sa.Column("include", ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("exclude", ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "connection_scope_skips",
        sa.Column("tenant_id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("connection", sa.String(), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
    )
    for table in _SCOPE_TABLES:
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation_{table} ON {table} "
                f"USING ({_TENANT_OR_BYPASS}) WITH CHECK ({_TENANT_OR_BYPASS})"
            )
        )
        op.execute(
            sa.text(
                _if_app_role(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO checkcord_app;")
            )
        )


def _scim_groups() -> None:
    op.create_table(
        "scim_groups",
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_scim_groups_name "
            "ON scim_groups (tenant_id, lower(display_name))"
        )
    )
    op.create_table(
        "scim_group_members",
        sa.Column("tenant_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column(
            "group_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("scim_groups.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("enterprise_users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_index("ix_scim_group_members_user", "scim_group_members", ["user_id"])
    for table in _SCIM_TABLES:
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation_{table} ON {table} "
                f"USING ({_TENANT_OR_BYPASS}) WITH CHECK ({_TENANT_OR_BYPASS})"
            )
        )
        op.execute(
            sa.text(
                _if_app_role(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO checkcord_app;")
            )
        )


def _drop_empty_event_partitions() -> None:
    op.execute(sa.text(_DROP_EMPTY))
    op.execute(
        sa.text("REVOKE ALL ON FUNCTION checkcord_drop_empty_events_partitions(date) FROM PUBLIC")
    )
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'checkcord_app') THEN
                    GRANT EXECUTE ON FUNCTION checkcord_drop_empty_events_partitions(date)
                        TO checkcord_app;
                END IF;
            END
            $$;
            """
        )
    )


def _connector_installations() -> None:
    op.create_table(
        _CONNECTOR_TABLE,
        sa.Column(
            "id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid()
        ),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("identifier", sa.String(), nullable=False),
        sa.Column("secret", sa.String(), nullable=True),
        sa.Column("config", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("tenant_id", "provider", "identifier"),
    )
    op.execute(sa.text(f"ALTER TABLE {_CONNECTOR_TABLE} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {_CONNECTOR_TABLE} FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation_{_CONNECTOR_TABLE} ON {_CONNECTOR_TABLE} "
            f"USING ({_TENANT_OR_BYPASS}) WITH CHECK ({_TENANT_OR_BYPASS})"
        )
    )
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'checkcord_app') THEN
                    GRANT SELECT, INSERT, UPDATE, DELETE ON {_CONNECTOR_TABLE} TO checkcord_app;
                END IF;
            END
            $$;
            """
        )
    )


def _record_update_excerpts() -> None:
    op.add_column("context_event_links", sa.Column("excerpt_redacted", sa.Text(), nullable=True))
    op.add_column("context_event_links", sa.Column("excerpt_version", sa.String(), nullable=True))
    op.add_column(
        "context_event_links",
        sa.Column("excerpt_pending", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_context_event_links_excerpt_pending ON context_event_links "
            "(tenant_id, canonical_id, event_id) WHERE excerpt_pending"
        )
    )


def _task_poll_cursors() -> None:
    for table in _POLL_CURSOR_TABLES:
        op.add_column(table, sa.Column("poll_cursor", sa.DateTime(timezone=True), nullable=True))
    op.add_column("notion_installations", sa.Column("poll_resume", sa.String(), nullable=True))


def _accounts_and_base_events() -> None:
    _bypass()
    op.add_column("chat_users", sa.Column("link_source", sa.String(), nullable=True))
    op.add_column("chat_users", sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("chat_users", sa.Column("linked_by", sa.String(), nullable=True))
    op.create_check_constraint(
        "ck_chat_users_link_source",
        "chat_users",
        "link_source IS NULL OR link_source IN ('email', 'profile', 'manual')",
    )
    op.execute(
        sa.text(
            "UPDATE chat_users SET link_source = 'email', linked_at = updated_at "
            "WHERE internal_id IS NOT NULL"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_chat_users_tenant_lower_email ON chat_users "
            "(tenant_id, lower(email)) WHERE email IS NOT NULL"
        )
    )
    op.create_index("ix_chat_users_internal_id", "chat_users", ["internal_id"])

    op.add_column(
        "audience_containers",
        sa.Column("unmatched_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "audience_containers",
        sa.Column("unmatched_accounts", ARRAY(sa.String()), nullable=False, server_default="{}"),
    )

    op.alter_column("canonical_contexts", "origin_event_id", new_column_name="base_event_id")
    op.execute(
        sa.text(
            "ALTER INDEX ix_canonical_contexts_origin_event_id "
            "RENAME TO ix_canonical_contexts_base_event_id"
        )
    )
    op.execute(sa.text(_BACKFILL_BASE))
    op.execute(sa.text(_FROM_BASE))

    op.create_table(
        "connection_audiences",
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("connection", sa.String(), nullable=False),
        sa.Column("container", sa.String(), nullable=True),
        sa.Column("mode", sa.String(), nullable=False, server_default="nobody"),
        sa.Column("groups", ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("people", ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("member_ids", ARRAY(PG_UUID(as_uuid=True)), nullable=False, server_default="{}"),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("recompute_state", sa.String(), nullable=True),
        sa.Column("recompute_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recompute_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recomputed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "mode IN ('nobody', 'everyone', 'groups', 'people')",
            name="ck_connection_audiences_mode",
        ),
    )
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_connection_audiences_key ON connection_audiences "
            "(tenant_id, connection, COALESCE(container, ''))"
        )
    )
    op.execute(sa.text("ALTER TABLE connection_audiences ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE connection_audiences FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation_connection_audiences ON connection_audiences "
            f"USING ({_TENANT_OR_BYPASS}) WITH CHECK ({_TENANT_OR_BYPASS})"
        )
    )
    op.execute(
        sa.text(
            _if_app_role(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON connection_audiences TO checkcord_app;"
            )
        )
    )


def _removed_sources() -> None:
    op.create_table(
        "removed_sources",
        sa.Column("tenant_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("source_platform", sa.Text(), nullable=False),
        sa.Column("source_id_hash", sa.Text(), nullable=False),
        sa.Column(
            "removed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "source_platform", "source_id_hash"),
    )
    op.execute(sa.text("ALTER TABLE removed_sources ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE removed_sources FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation_removed_sources ON removed_sources "
            f"USING ({_TENANT_OR_BYPASS}) WITH CHECK ({_TENANT_OR_BYPASS})"
        )
    )
    op.execute(
        sa.text(_if_app_role("GRANT SELECT, INSERT, DELETE ON removed_sources TO checkcord_app;"))
    )
