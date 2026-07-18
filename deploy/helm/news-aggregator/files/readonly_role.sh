#!/bin/sh
# Read-only API role (PLAN §3.1/§6.8): the zone-present API authenticates as
# api_ro, which can SELECT and nothing else — enforced at the DB grant, with the
# NetworkPolicy only scoping the route. Runs at FIRST initdb only (entrypoint
# convention), ordered after 10_schema.sql so the tables already exist.
# POSTGRES_RO_PASSWORD comes from the generated-in-cluster credentials (PLAN §3.5).
set -eu
: "${POSTGRES_RO_PASSWORD:?POSTGRES_RO_PASSWORD not set}"

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
CREATE ROLE api_ro LOGIN PASSWORD '$POSTGRES_RO_PASSWORD';
GRANT CONNECT ON DATABASE $POSTGRES_DB TO api_ro;
GRANT USAGE ON SCHEMA public TO api_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO api_ro;
-- Partitions created later (CronJob / workers, all as $POSTGRES_USER) stay readable:
ALTER DEFAULT PRIVILEGES FOR ROLE $POSTGRES_USER IN SCHEMA public
    GRANT SELECT ON TABLES TO api_ro;
SQL
