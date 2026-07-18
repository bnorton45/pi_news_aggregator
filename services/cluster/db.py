"""Cluster DB layer (PLAN §6.4): partition-pruned ANN + Story persistence.

Operates on *structured* fields (vector, entities, timestamps) with parameterized SQL —
it does not run a model on raw text, so it carries the same low risk as the db-writer
(PLAN §3.3); the LLM claim-extraction is isolated to the no-DB claimx worker.

The ANN query restricts `ts_observed >= since`, which prunes to the recent day
partitions (PLAN §6.4: per-partition HNSW, query the recent 1-2, not all 5).
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from uuid import UUID

import asyncpg
import numpy as np
from pgvector.asyncpg import register_vector

# Nearest already-clustered items in the time window, excluding the item itself.
_NEAREST = """
SELECT story_id, 1 - (embedding <=> $1) AS sim, entities
FROM items
WHERE story_id IS NOT NULL AND ts_observed >= $2 AND id <> $3 AND embedding IS NOT NULL
ORDER BY embedding <=> $1
LIMIT $4
"""

_ASSIGN = (
    "UPDATE items SET story_id = $1, simhash = $4, wire_ref = $5 "
    "WHERE id = $2 AND ts_observed = $3"
)

_CREATE_STORY = """
INSERT INTO stories (id, first_seen, last_seen, entity_set, source_set,
                     independent_origins, centroid)
VALUES ($1, $2, $2, $3::jsonb, $4::jsonb, 1, $5)
ON CONFLICT DO NOTHING
"""

# Provenance-relevant projection of a Story's members (PLAN §6.5). No time filter:
# everything alive is inside the 5-day wall by construction.
_STORY_PROV = """
SELECT id, author_ref, parent_ref, content_hash, urls, simhash, source,
       source_class::text, wire_ref, ts_observed
FROM items WHERE story_id = $1
"""

_INSERT_EDGE = """
INSERT INTO provenance_edges (story_id, src_item, dst_item, edge_type, ts_observed)
VALUES ($1, $2, $3, $4::prov_edge_type, $5)
ON CONFLICT DO NOTHING
"""

_STORY_EDGES = (
    "SELECT src_item, dst_item, edge_type::text FROM provenance_edges WHERE story_id = $1"
)

_UPDATE_TRUST = """
UPDATE stories SET independent_origins = $2, trust_state = $3::trust_state WHERE id = $1
"""

_STORY_STATE = "SELECT trust_state::text FROM stories WHERE id = $1 LIMIT 1"


class ClusterDb:
    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or os.environ["POSTGRES_DSN"]
        self._pool: asyncpg.Pool | None = None
        self._ensured: set[str] = set()

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=1, max_size=8, init=register_vector
        )

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    @property
    def _p(self) -> asyncpg.Pool:
        assert self._pool is not None, "connect() first"
        return self._pool

    async def nearest(
        self, vec: np.ndarray, since: datetime, exclude_id: UUID, k: int
    ) -> list[asyncpg.Record]:
        async with self._p.acquire() as conn:
            return await conn.fetch(_NEAREST, vec, since, exclude_id, k)

    async def assign_item(
        self, item_id: UUID, ts_observed: datetime, story_id: UUID, simhash: int, wire: str
    ) -> bool:
        """Set the item's story_id + simhash + wire_ref. False if the row isn't stored
        yet (writer race) → the caller NAKs for redelivery (PLAN §3.3 seam is
        at-least-once)."""
        async with self._p.acquire() as conn:
            res = await conn.execute(_ASSIGN, story_id, item_id, ts_observed, simhash, wire)
        return res.rsplit(" ", 1)[-1] != "0"  # "UPDATE <n>"

    async def _ensure_story_partition(self, conn: asyncpg.Connection, day: date) -> None:
        key = day.isoformat()
        if key not in self._ensured:
            await conn.execute("SELECT ensure_stories_partition($1)", day)
            self._ensured.add(key)

    async def create_story(
        self,
        story_id: UUID,
        first_seen: datetime,
        entity_set: list[str],
        source: str,
        centroid: np.ndarray,
    ) -> None:
        async with self._p.acquire() as conn:
            await self._ensure_story_partition(conn, first_seen.date())
            await conn.execute(
                _CREATE_STORY,
                story_id,
                first_seen,
                json.dumps(sorted(entity_set)),
                json.dumps([source]),
                centroid,
            )

    async def touch_story(
        self, story_id: UUID, last_seen: datetime, source: str, entity_set: list[str]
    ) -> None:
        """Fold a new member's source/entities into an existing Story. The origin count
        is NOT touched here — it comes from the provenance graph (§6.5, update_trust)."""
        async with self._p.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT source_set, entity_set FROM stories WHERE id = $1 LIMIT 1", story_id
            )
            if row is None:
                return
            sources = set(json.loads(row["source_set"])) | {source}
            entities = set(json.loads(row["entity_set"])) | set(entity_set)
            await conn.execute(
                "UPDATE stories SET last_seen = GREATEST(last_seen, $2), source_set = $3::jsonb, "
                "entity_set = $4::jsonb WHERE id = $1",
                story_id,
                last_seen,
                json.dumps(sorted(sources)),
                json.dumps(sorted(entities)),
            )

    # ── Provenance / trust (PLAN §6.5) ───────────────────────────────────────

    async def story_prov(self, story_id: UUID) -> list[asyncpg.Record]:
        """All members' provenance projections (id, refs, urls, simhash, class)."""
        async with self._p.acquire() as conn:
            return await conn.fetch(_STORY_PROV, story_id)

    async def insert_edges(
        self, story_id: UUID, edges: list[tuple[UUID, UUID, str]], ts_observed: datetime
    ) -> None:
        if not edges:
            return
        async with self._p.acquire() as conn:
            await self._ensure_provenance_partition(conn, ts_observed.date())
            await conn.executemany(
                _INSERT_EDGE, [(story_id, s, d, t, ts_observed) for s, d, t in edges]
            )

    async def story_edges(self, story_id: UUID) -> list[asyncpg.Record]:
        async with self._p.acquire() as conn:
            return await conn.fetch(_STORY_EDGES, story_id)

    async def update_trust(self, story_id: UUID, origins: int, state: str) -> None:
        async with self._p.acquire() as conn:
            await conn.execute(_UPDATE_TRUST, story_id, origins, state)

    async def story_state(self, story_id: UUID) -> str:
        async with self._p.acquire() as conn:
            row = await conn.fetchrow(_STORY_STATE, story_id)
        return row["trust_state"] if row else "rumor"

    async def _ensure_provenance_partition(self, conn: asyncpg.Connection, day: date) -> None:
        key = f"prov:{day.isoformat()}"
        if key not in self._ensured:
            await conn.execute("SELECT ensure_provenance_partition($1)", day)
            self._ensured.add(key)
