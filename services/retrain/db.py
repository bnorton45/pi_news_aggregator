"""Retrain DB access — the `retrain_ro` role (PLAN §3.3, §6.3 step 2).

Minimal surface by grant, not by convention: SELECT on the `weak_labels` view only
(the view runs with owner rights, so no items-table grant is needed) plus INSERT/UPDATE
on `system_state` for the health beat — nothing else is reachable even if this pod is
compromised. asyncpg is imported lazily (deferred) so the pure trainer/eval code stays
importable without it (matches services/score/db.py)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from services.retrain.evalx import LabeledRow

if TYPE_CHECKING:
    import asyncpg


class RetrainDb:
    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or os.environ["POSTGRES_DSN"]
        self._conn: asyncpg.Connection | None = None

    async def connect(self) -> None:
        import asyncpg

        self._conn = await asyncpg.connect(self._dsn)

    async def fetch_labels(self, limit: int) -> list[LabeledRow]:
        """The most-recent `limit` labeled weak-label rows (NULL labels excluded)."""
        assert self._conn is not None
        rows = await self._conn.fetch(
            """
            SELECT ts_observed, text, label
            FROM weak_labels
            WHERE label IS NOT NULL
            ORDER BY ts_observed DESC
            LIMIT $1
            """,
            limit,
        )
        return [LabeledRow(r["ts_observed"], r["text"], int(r["label"])) for r in rows]

    async def upsert_system_state(self, key: str, value: dict) -> None:
        import json

        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO system_state (key, value, updated_at)
            VALUES ($1, $2::jsonb, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            key,
            json.dumps(value),
        )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
