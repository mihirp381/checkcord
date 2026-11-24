#!/bin/sh
# Runs once, on an empty data directory, from the postgres image's docker-entrypoint-initdb.d.
set -eu

: "${CHECKCORD_OWNER_DB_PASSWORD:?CHECKCORD_OWNER_DB_PASSWORD must be set}"
: "${CHECKCORD_APP_DB_PASSWORD:?CHECKCORD_APP_DB_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f /checkcord/roles.sql

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v owner_pw="$CHECKCORD_OWNER_DB_PASSWORD" -v app_pw="$CHECKCORD_APP_DB_PASSWORD" <<'SQL'
ALTER ROLE checkcord_owner PASSWORD :'owner_pw';
ALTER ROLE checkcord_app PASSWORD :'app_pw';
SQL
