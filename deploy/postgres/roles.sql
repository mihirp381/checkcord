-- Checkcord database roles: checkcord_owner runs migrations and owns the schema; checkcord_app is what
-- the API and worker use, and row-level security applies to it.
-- Run as a superuser (or your managed service's admin user) connected to the Checkcord database,
-- before the first `checkcord migrate`. Safe to re-run. Then set the passwords:
--   ALTER ROLE checkcord_owner PASSWORD '...';
--   ALTER ROLE checkcord_app PASSWORD '...';

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'checkcord_owner') THEN
        CREATE ROLE checkcord_owner LOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'checkcord_app') THEN
        CREATE ROLE checkcord_app LOGIN;
    END IF;
    -- Managed services: the admin must hold checkcord_owner to set its default privileges below.
    IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
        EXECUTE format('GRANT checkcord_owner TO %I', current_user);
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO checkcord_owner, checkcord_app', current_database());
END
$$;

DO $$
BEGIN
    IF (SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = 'checkcord_app') THEN
        ALTER ROLE checkcord_app NOSUPERUSER NOBYPASSRLS;
    END IF;
    -- Data migrations must see every tenant's rows.
    BEGIN
        ALTER ROLE checkcord_owner BYPASSRLS;
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE WARNING 'could not grant BYPASSRLS to checkcord_owner; run migrations as a role that bypasses row-level security';
    END;
END
$$;

CREATE EXTENSION IF NOT EXISTS vector;

GRANT USAGE, CREATE ON SCHEMA public TO checkcord_owner;
GRANT USAGE ON SCHEMA public TO checkcord_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO checkcord_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO checkcord_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO checkcord_app;

ALTER DEFAULT PRIVILEGES FOR ROLE checkcord_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO checkcord_app;
ALTER DEFAULT PRIVILEGES FOR ROLE checkcord_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO checkcord_app;
ALTER DEFAULT PRIVILEGES FOR ROLE checkcord_owner IN SCHEMA public
    GRANT EXECUTE ON FUNCTIONS TO checkcord_app;

-- The audit log is append-only for checkcord_app. Re-running this file must not undo that.
DO $$
BEGIN
    IF to_regclass('public.audit_log_entries') IS NOT NULL THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON audit_log_entries FROM checkcord_app;
    END IF;
END
$$;
-- Re-running this script must not undo grants that migrations narrowed.
DO $$
BEGIN
    IF to_regclass('public.audit_log_entries') IS NOT NULL THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON audit_log_entries FROM checkcord_app;
    END IF;
    IF to_regclass('public.scheduled_jobs') IS NOT NULL THEN
        REVOKE DELETE, TRUNCATE ON scheduled_jobs FROM checkcord_app;
    END IF;
END
$$;
