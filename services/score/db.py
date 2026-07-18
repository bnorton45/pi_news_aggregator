"""Score-worker DB layer (PLAN §6.6/§6.8).

Structured-data pod (PLAN §3.3): parameterized SQL over stored fields — no model,
no raw text, no NATS. Reads entity_tallies (firehose velocity signal) + stories,
writes the score columns back and raises alert rows.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import asyncpg

_ACTIVE_STORIES = """
SELECT id, first_seen, entity_set, trust_state::text, independent_origins
FROM stories
WHERE last_seen > now() - $1::interval
  AND entity_set <> '[]'::jsonb
ORDER BY last_seen DESC
LIMIT $2
"""

_EARLIEST_BUCKET = """
SELECT min(bucket_ts) FROM entity_tallies WHERE bucket_ts > now() - $1::interval
"""

# Sparse per-bin totals for a story's entity set, summed across replicas.
# date_bin aligns bins to the epoch; the reader gap-fills to a dense series.
_MENTION_SERIES = """
SELECT date_bin($3, bucket_ts, 'epoch'::timestamptz) AS bin, sum(mentions)::float AS n
FROM entity_tallies
WHERE entity = ANY($1::text[]) AND bucket_ts > $2
GROUP BY bin
ORDER BY bin
"""

_MAINSTREAM_COUNT = """
SELECT count(*) FROM items WHERE story_id = $1 AND source_class = 'mainstream'
"""

_UPDATE_SCORES = """
UPDATE stories
SET velocity_z = $2, mainstream_presence = $3, gap = $4, inauthenticity = $5
WHERE id = $1
"""

# ── §6.7 inauthenticity signals (see services/score/inauth.py) ────────────────
# Social item count + how many were observed within the sync window of the
# neighboring social item (bot fleets post together; organic arrivals spread).
_INAUTH_SYNC = """
WITH soc AS (
    SELECT ts_observed,
           lag(ts_observed)  OVER (ORDER BY ts_observed) AS prev,
           lead(ts_observed) OVER (ORDER BY ts_observed) AS nxt
    FROM items
    WHERE story_id = $1 AND source_class = 'social'
)
SELECT count(*)::int AS social_items,
       coalesce(count(*) FILTER (
           WHERE ts_observed - prev <= $2::interval
              OR nxt - ts_observed <= $2::interval
       ), 0)::int AS synced_items
FROM soc
"""

# Copypasta / same-account amplification leaves exactly these edge types
# (libs/trust/edges.py: COPY = simhash near-dup, AUTHOR = same author_ref).
_INAUTH_COPY_EDGES = """
SELECT count(*)::int FROM provenance_edges
WHERE story_id = $1 AND edge_type IN ('copy', 'author')
"""

# In-window repeat offenders (the §6.7 5d source reputation): authors of this
# story's social items whose window-wide social output ($3+ items to judge by)
# mostly landed in already-flagged stories ($2 = inauthenticity flag threshold,
# $4 = bad share). Unclustered items count as unflagged. Returns how many of
# THIS story's social items those authors wrote.
_INAUTH_LOW_REP = """
WITH story_authors AS (
    SELECT author_ref, count(*) AS story_items
    FROM items
    WHERE story_id = $1 AND source_class = 'social' AND author_ref <> ''
    GROUP BY author_ref
), rep AS (
    SELECT i.author_ref,
           count(*) AS n,
           avg(CASE WHEN st.inauthenticity >= $2 THEN 1.0 ELSE 0.0 END) AS bad_share
    FROM items i
    JOIN story_authors sa USING (author_ref)
    LEFT JOIN stories st ON st.id = i.story_id
    WHERE i.source_class = 'social'
    GROUP BY i.author_ref
)
SELECT coalesce(
    sum(sa.story_items) FILTER (WHERE rep.n >= $3 AND rep.bad_share >= $4), 0
)::int
FROM story_authors sa
JOIN rep USING (author_ref)
"""

# Alert with re-alert cooldown in one statement: no row lands if a recent alert
# for the story exists. At-most-once is NOT required (a duplicate alert is noise,
# not corruption), so no lock — worst case two replicas both alert.
_INSERT_ALERT = """
INSERT INTO alerts (story_id, gap, velocity_z, trust_state)
SELECT $1, $2, $3, $4::trust_state
WHERE NOT EXISTS (SELECT 1 FROM alerts WHERE story_id = $1 AND ts > now() - $5::interval)
"""

_UPSERT_STATE = """
INSERT INTO system_state (key, value) VALUES ($1, $2::jsonb)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
"""


class ScoreDb:
    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or os.environ["POSTGRES_DSN"]
        self._pool: asyncpg.Pool | None = None
        self._ensured: set[str] = set()

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    @property
    def _p(self) -> asyncpg.Pool:
        assert self._pool is not None, "connect() first"
        return self._pool

    async def active_stories(self, window: timedelta, limit: int) -> list[asyncpg.Record]:
        async with self._p.acquire() as conn:
            return await conn.fetch(_ACTIVE_STORIES, window, limit)

    async def earliest_bucket(self, baseline: timedelta) -> datetime | None:
        async with self._p.acquire() as conn:
            return await conn.fetchval(_EARLIEST_BUCKET, baseline)

    async def mention_series(
        self, entities: list[str], since: datetime, bin_width: timedelta
    ) -> dict[datetime, float]:
        async with self._p.acquire() as conn:
            rows = await conn.fetch(_MENTION_SERIES, entities, since, bin_width)
        return {r["bin"]: r["n"] for r in rows}

    async def mainstream_count(self, story_id: UUID) -> int:
        async with self._p.acquire() as conn:
            return await conn.fetchval(_MAINSTREAM_COUNT, story_id)

    async def update_scores(
        self,
        story_id: UUID,
        velocity_z: float,
        mainstream_presence: float,
        gap: float,
        inauthenticity: float,
    ) -> None:
        async with self._p.acquire() as conn:
            await conn.execute(
                _UPDATE_SCORES, story_id, velocity_z, mainstream_presence, gap, inauthenticity
            )

    async def inauth_counts(
        self,
        story_id: UUID,
        sync_window: timedelta,
        flag_threshold: float,
        rep_min_items: int,
        rep_bad_share: float,
    ) -> tuple[int, int, int, int]:
        """(social_items, synced_items, copy_edges, low_rep_items) for one Story —
        the DB half of the §6.7 signals; the math lives in services/score/inauth.py."""
        async with self._p.acquire() as conn:
            sync = await conn.fetchrow(_INAUTH_SYNC, story_id, sync_window)
            copy_edges = await conn.fetchval(_INAUTH_COPY_EDGES, story_id)
            low_rep = await conn.fetchval(
                _INAUTH_LOW_REP, story_id, flag_threshold, rep_min_items, rep_bad_share
            )
        return sync["social_items"], sync["synced_items"], copy_edges, low_rep

    async def insert_alert(
        self,
        story_id: UUID,
        gap: float,
        velocity_z: float,
        trust_state: str,
        cooldown: timedelta,
    ) -> bool:
        """Raise an alert unless one fired for this story within `cooldown`.
        Returns True when a new alert row landed."""
        async with self._p.acquire() as conn:
            await self._ensure_partition(conn, datetime.now(UTC).date())
            status = await conn.execute(
                _INSERT_ALERT, story_id, gap, velocity_z, trust_state, cooldown
            )
        return status.endswith("1")

    async def _ensure_partition(self, conn: asyncpg.Connection, day: date) -> None:
        key = day.isoformat()
        if key in self._ensured:
            return
        await conn.execute("SELECT ensure_alerts_partition($1)", day)
        self._ensured.add(key)

    async def upsert_system_state(self, key: str, value: dict) -> None:
        async with self._p.acquire() as conn:
            await conn.execute(_UPSERT_STATE, key, json.dumps(value))
