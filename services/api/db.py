"""Read-API DB layer (PLAN §6.8): SELECT-only, as the api_ro role.

Read-only is enforced three ways: the api_ro grant (the real fence), the pool's
default_transaction_read_only (belt-and-braces), and the zone-present netpol
(route scoping). Every query is parameterized over stored fields.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from uuid import UUID

import asyncpg

_STORY_COLS = """
SELECT s.id, s.first_seen, s.last_seen, s.entity_set, s.source_set,
       s.independent_origins, s.trust_state::text, s.velocity_z,
       s.mainstream_presence, s.inauthenticity, s.gap,
       (SELECT count(*) FROM items i WHERE i.story_id = s.id) AS item_count
FROM stories s
WHERE s.last_seen > now() - $1::interval
"""

# Weather (NOAA/NWS severe-weather alerts, source `noaa`) is authoritative but
# floods the news feed, so it lives on its own tab and every other tab excludes
# it. source_set is a jsonb array; the `?` operator tests element membership
# (asyncpg uses $n placeholders, so a bare `?` is the operator, not a param).
_NOT_WEATHER = "AND NOT (s.source_set ? 'noaa') "
_ORDER = "ORDER BY s.gap DESC, s.last_seen DESC LIMIT $2"

# Default news feed: highest gap first (the §6.6 target signal), weather aside.
_STORIES = _STORY_COLS + _NOT_WEATHER + _ORDER

# Corroboration watchlist (§6.5): stories that reached exactly `corroborated` but
# may sit at gap≈0 (mainstream already present, or velocity low) — the default
# gap feed buries them until they age out. This view keeps that evidence visible.
# Primary-backed is a distinct tab, so this filter is `= 'corroborated'`, not `>=`.
_STORIES_CORROBORATED = _STORY_COLS + "AND s.trust_state = 'corroborated' " + _NOT_WEATHER + _ORDER

# Primary-backed watchlist: stories corroborated by a primary/authoritative source.
_STORIES_PRIMARY = _STORY_COLS + "AND s.trust_state = 'primary_backed' " + _NOT_WEATHER + _ORDER

# Weather tab: the noaa complement of the news feed, gap-ranked among themselves.
_STORIES_WEATHER = _STORY_COLS + "AND s.source_set ? 'noaa' " + _ORDER

STORY_QUERIES = {
    "gap": _STORIES,
    "corroborated": _STORIES_CORROBORATED,
    "primary": _STORIES_PRIMARY,
    "weather": _STORIES_WEATHER,
}

_STORY = """
SELECT s.id, s.first_seen, s.last_seen, s.entity_set, s.source_set,
       s.independent_origins, s.trust_state::text, s.velocity_z,
       s.mainstream_presence, s.inauthenticity, s.gap
FROM stories s WHERE s.id = $1
"""

# The feed shows text — that is the product — but capped so one giant item
# cannot balloon a response.
_STORY_ITEMS = """
SELECT id, source, source_class::text, ts_observed, lang,
       left(text, 500) AS text, urls, author_ref, geo
FROM items WHERE story_id = $1 ORDER BY ts_observed DESC LIMIT $2
"""

_VELOCITY_SERIES = """
SELECT date_bin($3, bucket_ts, 'epoch'::timestamptz) AS bin, sum(mentions)::float AS n
FROM entity_tallies
WHERE entity = ANY($1::text[]) AND bucket_ts > now() - $2::interval
GROUP BY bin
ORDER BY bin
"""

_ALERTS = """
SELECT a.story_id, a.ts, a.gap, a.velocity_z, a.trust_state::text, s.entity_set
FROM alerts a LEFT JOIN stories s ON s.id = a.story_id
WHERE a.ts > now() - $1::interval
ORDER BY a.ts DESC
LIMIT $2
"""

_SYSTEM_STATE = "SELECT key, value, updated_at FROM system_state ORDER BY key"


class ReadDb:
    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or os.environ["POSTGRES_DSN"]
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=4,
            server_settings={"default_transaction_read_only": "on"},
        )

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    @property
    def _p(self) -> asyncpg.Pool:
        if self._pool is None:  # startup race / failover window (§6.8)
            raise ConnectionError("db pool not connected yet")
        return self._pool

    async def stories(
        self, window: timedelta, limit: int, sort: str = "gap"
    ) -> list[asyncpg.Record]:
        query = STORY_QUERIES.get(sort, _STORIES)
        async with self._p.acquire() as conn:
            return await conn.fetch(query, window, limit)

    async def story(self, story_id: UUID) -> asyncpg.Record | None:
        async with self._p.acquire() as conn:
            return await conn.fetchrow(_STORY, story_id)

    async def story_items(self, story_id: UUID, limit: int) -> list[asyncpg.Record]:
        async with self._p.acquire() as conn:
            return await conn.fetch(_STORY_ITEMS, story_id, limit)

    async def velocity_series(
        self, entities: list[str], window: timedelta, bin_width: timedelta
    ) -> list[asyncpg.Record]:
        async with self._p.acquire() as conn:
            return await conn.fetch(_VELOCITY_SERIES, entities, window, bin_width)

    async def alerts(self, window: timedelta, limit: int) -> list[asyncpg.Record]:
        async with self._p.acquire() as conn:
            return await conn.fetch(_ALERTS, window, limit)

    async def system_state(self) -> list[asyncpg.Record]:
        async with self._p.acquire() as conn:
            return await conn.fetch(_SYSTEM_STATE)

    async def now(self) -> datetime:
        async with self._p.acquire() as conn:
            return await conn.fetchval("SELECT now()")
