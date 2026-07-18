"""Postgres writer for enriched Items (PLAN §4, §6.2).

Ensures the daily partition exists before insert (provisioned ahead by the CronJob,
but this is a belt-and-braces safety net), then writes the Item + embedding into the
partitioned pgvector table. ON CONFLICT DO NOTHING makes redelivery idempotent.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date

import asyncpg
import numpy as np
from pgvector.asyncpg import register_vector

from libs.schema import Item, TallyFlush

log = logging.getLogger("enrich.db")

_INSERT = """
INSERT INTO items (id, source, source_class, ts_observed, ts_event, lang, text,
                   entities, geo, urls, author_ref, parent_ref, content_hash,
                   raw_ref, embedding, exploration)
VALUES ($1, $2, $3::source_class, $4, $5, $6, $7,
        $8::jsonb, $9::jsonb, $10::jsonb, $11, $12, $13, $14, $15, $16)
ON CONFLICT DO NOTHING
"""

_INSERT_TALLY = """
INSERT INTO entity_tallies (entity, bucket_ts, replica, mentions)
VALUES ($1, $2, $3, $4)
ON CONFLICT DO NOTHING
"""

_UPSERT_STATE = """
INSERT INTO system_state (key, value) VALUES ($1, $2::jsonb)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
"""


class Db:
    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or os.environ["POSTGRES_DSN"]
        self._pool: asyncpg.Pool | None = None
        self._ensured: set[str] = set()  # partition dates already provisioned this run

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=1, max_size=8, init=register_vector
        )

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    async def _ensure_partition(
        self, conn: asyncpg.Connection, day: date, fn: str = "ensure_items_partition"
    ) -> None:
        key = f"{fn}:{day.isoformat()}"
        if key in self._ensured:
            return
        await conn.execute(f"SELECT {fn}($1)", day)  # fn is an internal literal, never input
        self._ensured.add(key)

    async def insert_item(
        self, item: Item, embedding: np.ndarray, *, exploration: bool = False
    ) -> None:
        assert self._pool is not None, "connect() first"
        day = item.ts_observed.date()
        geo = item.geo.model_dump() if item.geo else None
        async with self._pool.acquire() as conn:
            await self._ensure_partition(conn, day)
            await conn.execute(
                _INSERT,
                item.id,
                item.source,
                item.source_class.value,
                item.ts_observed,
                item.ts_event,
                item.lang,
                item.text,
                json.dumps([e.model_dump() for e in item.entities]),
                json.dumps(geo) if geo is not None else None,
                json.dumps(item.urls),
                item.author_ref,
                item.parent_ref,
                item.content_hash,
                item.raw_ref,
                embedding,
                exploration,
            )

    async def insert_tallies(self, flush: TallyFlush) -> None:
        """Persist one enrich-replica tally flush (PLAN §6.6). The (entity, bucket_ts,
        replica) PK + DO NOTHING make JetStream at-least-once redelivery idempotent."""
        assert self._pool is not None, "connect() first"
        if not flush.counts:
            return
        rows = [(e, flush.bucket_ts, flush.replica, n) for e, n in flush.counts.items()]
        async with self._pool.acquire() as conn:
            await self._ensure_partition(conn, flush.bucket_ts.date(), "ensure_tallies_partition")
            await conn.executemany(_INSERT_TALLY, rows)

    async def upsert_system_state(self, key: str, value: dict) -> None:
        """§6.8 health beat: tiny key/value, no content — deliberately outside the
        partitioned 5-day wall (system_state is unwalled)."""
        assert self._pool is not None, "connect() first"
        async with self._pool.acquire() as conn:
            await conn.execute(_UPSERT_STATE, key, json.dumps(value))
