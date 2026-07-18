-- Postgres 16 + pgvector schema (PLAN §4, §6.2, §6.4).
--
-- Data-lifecycle hard rule: everything content-bearing is gone at 5 days via
-- DAILY DECLARATIVE PARTITIONS that are DROPped (never DELETEd — avoids bloat /
-- VACUUM write-amplification, PLAN §4). Each daily partition carries its own HNSW
-- index, which also realizes the per-partition ANN design (PLAN §6.4).

CREATE EXTENSION IF NOT EXISTS vector;

-- Embedding dimension for the local quantized model (MiniLM / BGE-small class).
-- If the model changes dimensionality, this and the column type change together.
-- (kept as a comment anchor; column below hardcodes 384)

DO $$ BEGIN
    -- 'local_news' added pre-ingester (§6.5 wire collapse; docs/local-news-design.md).
    -- First-initdb-only, like everything here: existing dev volumes need a recreate.
    CREATE TYPE source_class AS ENUM ('authoritative', 'primary', 'social', 'mainstream', 'local_news');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE trust_state AS ENUM ('rumor', 'corroborated', 'primary_backed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE prov_edge_type AS ENUM ('ref', 'copy', 'url', 'author');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ── Items (partitioned by observation date) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS items (
    id            uuid        NOT NULL,
    source        text        NOT NULL,
    source_class  source_class NOT NULL,
    ts_observed   timestamptz NOT NULL,
    ts_event      timestamptz,
    lang          text        NOT NULL DEFAULT 'und',
    text          text        NOT NULL,
    entities      jsonb       NOT NULL DEFAULT '[]',
    geo           jsonb,
    urls          jsonb       NOT NULL DEFAULT '[]',
    author_ref    text        NOT NULL DEFAULT '',
    parent_ref    text,
    content_hash  text        NOT NULL DEFAULT '',
    raw_ref       text        NOT NULL DEFAULT '',
    -- embedding is set by the enrich layer; survivors only (filter-then-embed §6.3)
    embedding     vector(384),
    -- story_id is set by the cluster layer (§6.4) after the item is stored; NULL = unclustered
    story_id      uuid,
    -- simhash is set with story_id by the cluster layer (§6.5 edge b); signed 64-bit
    simhash       bigint,
    -- wire_ref is set with story_id/simhash by the cluster layer: credited wire
    -- service slug ('ap', 'reuters', …) for §6.5 syndication collapse; '' = none
    wire_ref      text        NOT NULL DEFAULT '',
    -- §6.3a exploration quota: embedded from BELOW the admission threshold — a
    -- shed-tail counterfactual for the filter retrain loop, not a real admission
    exploration   boolean     NOT NULL DEFAULT false,
    PRIMARY KEY (id, ts_observed)
) PARTITION BY RANGE (ts_observed);

-- Parent-level indexes propagate to every partition.
CREATE INDEX IF NOT EXISTS items_ts_observed_idx ON items (ts_observed);
CREATE INDEX IF NOT EXISTS items_content_hash_idx ON items (content_hash);
CREATE INDEX IF NOT EXISTS items_source_idx ON items (source);
CREATE INDEX IF NOT EXISTS items_story_id_idx ON items (story_id);

-- ── Stories (partitioned by first_seen date; age out with their Items) ───────
CREATE TABLE IF NOT EXISTS stories (
    id                  uuid        NOT NULL,
    first_seen          timestamptz NOT NULL,
    last_seen           timestamptz NOT NULL,
    entity_set          jsonb       NOT NULL DEFAULT '[]',
    source_set          jsonb       NOT NULL DEFAULT '[]',
    independent_origins int         NOT NULL DEFAULT 0,
    trust_state         trust_state NOT NULL DEFAULT 'rumor',
    velocity_z          double precision NOT NULL DEFAULT 0,
    mainstream_presence double precision NOT NULL DEFAULT 0,
    inauthenticity      double precision NOT NULL DEFAULT 0,
    gap                 double precision NOT NULL DEFAULT 0,
    centroid            vector(384),
    PRIMARY KEY (id, first_seen)
) PARTITION BY RANGE (first_seen);

-- ── Firehose entity tallies (PLAN §6.6; partitioned, gone at the 5-day wall) ──
-- Per-minute gazetteer mention deltas flushed by enrich replicas over NATS and
-- persisted by the db-writer. Velocity reads these — the FULL-firehose signal,
-- immune to §6.3a admission sampling. PK includes replica so at-least-once
-- redelivery is idempotent (DO NOTHING on conflict).
CREATE TABLE IF NOT EXISTS entity_tallies (
    entity     text        NOT NULL,
    bucket_ts  timestamptz NOT NULL,
    replica    text        NOT NULL DEFAULT '',
    mentions   int         NOT NULL,
    PRIMARY KEY (entity, bucket_ts, replica)
) PARTITION BY RANGE (bucket_ts);

CREATE INDEX IF NOT EXISTS entity_tallies_bucket_idx ON entity_tallies (bucket_ts);

-- ── Alerts (PLAN §6.6/§6.8; partitioned, gone at the 5-day wall) ─────────────
-- One row per alert firing: gap > threshold ∧ trust_state ≥ corroborated, raised
-- by the score worker with a re-alert cooldown. References the Story by id only.
CREATE TABLE IF NOT EXISTS alerts (
    story_id    uuid             NOT NULL,
    ts          timestamptz      NOT NULL DEFAULT now(),
    gap         double precision NOT NULL,
    velocity_z  double precision NOT NULL,
    trust_state trust_state      NOT NULL,
    PRIMARY KEY (story_id, ts)
) PARTITION BY RANGE (ts);

CREATE INDEX IF NOT EXISTS alerts_ts_idx ON alerts (ts);

-- ── Weak labels (PLAN §6.3 step 2) — a VIEW, so it lives inside the 5-day wall ─
-- Training rows for the filter retrain loop, derived in-window (the wall forbids
-- a growing corpus): social members of §6.7-flagged Stories = CIB negatives
-- (checked FIRST — CIB that fooled corroboration must stay negative; the 0.7
-- literal mirrors INAUTH_FLAG_THRESHOLD in services/score/inauth.py); members of
-- corroborated/primary-backed Stories = positive; exploration items
-- (below-threshold embeds) that never corroborated = negative counterfactuals;
-- everything else = unlabeled.
CREATE OR REPLACE VIEW weak_labels AS
SELECT i.id,
       i.ts_observed,
       i.lang,
       i.text,
       i.exploration,
       CASE
           WHEN s.inauthenticity >= 0.7 AND i.source_class = 'social' THEN 0
           WHEN s.trust_state IN ('corroborated', 'primary_backed') THEN 1
           WHEN i.exploration THEN 0
           ELSE NULL
       END AS label
FROM items i
LEFT JOIN stories s ON s.id = i.story_id;

-- ── System health state (PLAN §6.8) — tiny key/value, no content, not walled ─
CREATE TABLE IF NOT EXISTS system_state (
    key        text        PRIMARY KEY,
    value      jsonb       NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ── Provenance edges (PLAN §6.5; partitioned like items, gone at the 5-day wall)
-- A row = "src depends on dst" (or vice versa — direction is not load-bearing for
-- weakly-connected components). ts_observed = the *new* item's observation time so
-- edges age out no later than the members they reference.
CREATE TABLE IF NOT EXISTS provenance_edges (
    story_id     uuid           NOT NULL,
    src_item     uuid           NOT NULL,
    dst_item     uuid           NOT NULL,
    edge_type    prov_edge_type NOT NULL,
    ts_observed  timestamptz    NOT NULL,
    PRIMARY KEY (story_id, src_item, dst_item, edge_type, ts_observed)
) PARTITION BY RANGE (ts_observed);

CREATE INDEX IF NOT EXISTS provenance_edges_story_idx ON provenance_edges (story_id);

-- ── Partition management ─────────────────────────────────────────────────────
-- Create the daily partition covering `day` if absent. Idempotent.
-- Each partition gets its own HNSW index (per-partition ANN, PLAN §6.4).
CREATE OR REPLACE FUNCTION ensure_items_partition(day date) RETURNS void AS $$
DECLARE
    part  text := 'items_' || to_char(day, 'YYYYMMDD');
    nextd date := day + 1;
BEGIN
    IF to_regclass(part) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF items FOR VALUES FROM (%L) TO (%L)',
            part, day::timestamptz, nextd::timestamptz);
        EXECUTE format(
            'CREATE INDEX %I ON %I USING hnsw (embedding vector_cosine_ops)',
            part || '_hnsw', part);
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION ensure_stories_partition(day date) RETURNS void AS $$
DECLARE
    part  text := 'stories_' || to_char(day, 'YYYYMMDD');
    nextd date := day + 1;
BEGIN
    IF to_regclass(part) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF stories FOR VALUES FROM (%L) TO (%L)',
            part, day::timestamptz, nextd::timestamptz);
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION ensure_tallies_partition(day date) RETURNS void AS $$
DECLARE
    part  text := 'entity_tallies_' || to_char(day, 'YYYYMMDD');
    nextd date := day + 1;
BEGIN
    IF to_regclass(part) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF entity_tallies FOR VALUES FROM (%L) TO (%L)',
            part, day::timestamptz, nextd::timestamptz);
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION ensure_alerts_partition(day date) RETURNS void AS $$
DECLARE
    part  text := 'alerts_' || to_char(day, 'YYYYMMDD');
    nextd date := day + 1;
BEGIN
    IF to_regclass(part) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF alerts FOR VALUES FROM (%L) TO (%L)',
            part, day::timestamptz, nextd::timestamptz);
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION ensure_provenance_partition(day date) RETURNS void AS $$
DECLARE
    part  text := 'provenance_edges_' || to_char(day, 'YYYYMMDD');
    nextd date := day + 1;
BEGIN
    IF to_regclass(part) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF provenance_edges FOR VALUES FROM (%L) TO (%L)',
            part, day::timestamptz, nextd::timestamptz);
    END IF;
END;
$$ LANGUAGE plpgsql;

-- Provision partitions for [today - back, today + ahead]. Run by a CronJob daily
-- so tomorrow's partition always exists before midnight.
CREATE OR REPLACE FUNCTION provision_partitions(back int DEFAULT 5, ahead int DEFAULT 1)
RETURNS void AS $$
DECLARE d date;
BEGIN
    FOR d IN SELECT generate_series(
        (current_date - back), (current_date + ahead), interval '1 day')::date
    LOOP
        PERFORM ensure_items_partition(d);
        PERFORM ensure_stories_partition(d);
        PERFORM ensure_provenance_partition(d);
        PERFORM ensure_tallies_partition(d);
        PERFORM ensure_alerts_partition(d);
    END LOOP;
END;
$$ LANGUAGE plpgsql;

-- The 5-day wall (PLAN §4): DROP partitions strictly older than retention_days.
-- DROP (not DELETE) avoids bloat/VACUUM write-amplification.
CREATE OR REPLACE FUNCTION drop_old_partitions(retention_days int DEFAULT 5)
RETURNS void AS $$
DECLARE
    rec    record;
    cutoff date := current_date - retention_days;
    pday   date;
BEGIN
    FOR rec IN
        SELECT c.relname
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname IN ('items', 'stories', 'provenance_edges', 'entity_tallies', 'alerts')
    LOOP
        BEGIN
            pday := to_date(right(rec.relname, 8), 'YYYYMMDD');
        EXCEPTION WHEN others THEN
            CONTINUE;  -- skip anything not matching the naming convention
        END;
        IF pday < cutoff THEN
            EXECUTE format('DROP TABLE IF EXISTS %I', rec.relname);
        END IF;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

-- Bootstrap: make sure the current window exists immediately.
SELECT provision_partitions();
