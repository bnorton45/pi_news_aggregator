#!/bin/sh
# Retrain role (PLAN §3.3/§6.3 step 2): the zone-process retrain worker authenticates
# as retrain_ro, which can SELECT the `weak_labels` VIEW (executed with the owner's
# rights, so NO grant on items/stories is needed or given) and write ONLY the
# `system_state` health beat — nothing else is reachable even if the pod is exploited.
# Runs at FIRST initdb only (entrypoint convention), ordered after 10_schema.sql so the
# view and system_state already exist. POSTGRES_RETRAIN_PASSWORD comes from the
# generated-in-cluster credentials (PLAN §3.5).
set -eu
: "${POSTGRES_RETRAIN_PASSWORD:?POSTGRES_RETRAIN_PASSWORD not set}"

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
CREATE ROLE retrain_ro LOGIN PASSWORD '$POSTGRES_RETRAIN_PASSWORD';
GRANT CONNECT ON DATABASE $POSTGRES_DB TO retrain_ro;
GRANT USAGE ON SCHEMA public TO retrain_ro;
-- weak_labels is a VIEW: owner-rights execution means view SELECT alone reads the
-- items/stories underneath, with no direct table grant (least privilege, PLAN §3.3).
GRANT SELECT ON weak_labels TO retrain_ro;
-- The one write: its own health beat, via INSERT..ON CONFLICT DO UPDATE. That upsert
-- reads the `key` column to resolve the conflict, so SELECT is required alongside
-- INSERT/UPDATE. system_state is the non-content health table api_ro already reads
-- (schema.sql) — granting SELECT here adds no content reach.
GRANT SELECT, INSERT, UPDATE ON system_state TO retrain_ro;
SQL
