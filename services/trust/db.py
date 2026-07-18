"""Trust-worker DB layer (PLAN §6.5 primary-match).

Structured-data pod (PLAN §3.3): parameterized SQL over stored fields — it never
runs a model, so DB creds are in-posture (same class as writer/cluster). The claim
text itself is NOT queried with; only the claiming item's already-extracted
entities/geo are (set by enrich's cheap NER, §6.3 step 4).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from uuid import UUID

import asyncpg

_CLAIM_ITEM = """
SELECT id, source_class::text, entities, geo, ts_observed, story_id
FROM items WHERE id = $1
"""

# Coarse candidate prefilter: primary/authoritative class, inside the time window,
# sharing at least one case-folded entity surface form. Fine alignment (geo) is
# decided in services.trust.match.
_CANDIDATES = """
SELECT id, entities, geo
FROM items
WHERE source_class IN ('authoritative', 'primary')
  AND ts_observed BETWEEN $1 AND $2
  AND EXISTS (
      SELECT 1 FROM jsonb_array_elements(entities) e
      WHERE lower(e->>'text') = ANY($3::text[])
  )
ORDER BY ts_observed DESC
LIMIT $4
"""

# Monotonic by construction: primary_backed is the top state, so an unconditional
# set can never demote (§6.5 states).
_PROMOTE = "UPDATE stories SET trust_state = 'primary_backed' WHERE id = $1"


class TrustDb:
    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or os.environ["POSTGRES_DSN"]
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    @property
    def _p(self) -> asyncpg.Pool:
        assert self._pool is not None, "connect() first"
        return self._pool

    async def claim_item(self, item_id: UUID) -> asyncpg.Record | None:
        async with self._p.acquire() as conn:
            return await conn.fetchrow(_CLAIM_ITEM, item_id)

    async def primary_candidates(
        self, around: datetime, window: timedelta, entities: list[str], limit: int = 50
    ) -> list[asyncpg.Record]:
        async with self._p.acquire() as conn:
            return await conn.fetch(_CANDIDATES, around - window, around + window, entities, limit)

    async def promote_primary_backed(self, story_id: UUID) -> None:
        async with self._p.acquire() as conn:
            await conn.execute(_PROMOTE, story_id)
