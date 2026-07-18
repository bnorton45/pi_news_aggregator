"""Alert push notifier (PLAN §6.8): alerts table → self-hosted ntfy.

zone-present worker on the READ-ONLY api_ro role: polls the alerts table (raised
by the score worker, §6.6) and POSTs each new row to the in-cluster ntfy server.
No DB writes (the cursor is in-memory and boots at the newest existing alert —
no replay storm), no NATS, no internet egress: the netpol opens exactly
Postgres:5432 and the ntfy Service, nothing else, and ntfy itself is LAN-only
behind traefik.

Delivery is at-least-once: the cursor advances only past alerts that POSTed
successfully, so a ntfy outage replays the tail next cycle (a duplicate push is
noise, not corruption — same stance as the alert insert itself).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:  # deferred: keeps the pure push logic importable without asyncpg
    import asyncpg

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("notify")

POLL_S = float(os.environ.get("NOTIFY_POLL_S", "20"))
BATCH = int(os.environ.get("NOTIFY_BATCH", "50"))
NTFY_URL = os.environ.get("NTFY_URL", "http://ntfy:8080")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "alerts")
NTFY_USER = os.environ.get("NTFY_USER", "publisher")
NTFY_PASSWORD = os.environ.get("NTFY_PASSWORD", "")

_NEW_ALERTS = """
SELECT a.story_id, a.ts, a.gap, a.velocity_z, a.trust_state::text AS trust_state,
       s.entity_set
FROM alerts a
LEFT JOIN stories s ON s.id = a.story_id
WHERE a.ts > $1
ORDER BY a.ts
LIMIT $2
"""

_NEWEST_TS = "SELECT max(ts) FROM alerts"


def format_push(row: Any) -> tuple[str, str]:
    """(title, body) for one alert row — compact, glanceable on a phone."""
    entities = ", ".join(json.loads(row["entity_set"] or "[]")[:5]) or "unknown entities"
    title = f"Developing story: {entities}"
    body = (
        f"gap {row['gap']:.1f} · velocity z {row['velocity_z']:.1f} · "
        f"{row['trust_state']} · story {row['story_id']}"
    )
    return title, body


class Notifier:
    def __init__(self, pool: asyncpg.Pool, client: httpx.AsyncClient) -> None:
        self.pool = pool
        self.client = client
        self.cursor: datetime | None = None  # set on first cycle from max(ts)
        self.pushed = 0

    async def push(self, row: asyncpg.Record) -> None:
        title, body = format_push(row)
        r = await self.client.post(
            f"{NTFY_URL}/{NTFY_TOPIC}",
            content=body.encode(),
            headers={"Title": title, "Priority": "high", "Tags": "rotating_light"},
            auth=(NTFY_USER, NTFY_PASSWORD),
        )
        r.raise_for_status()

    async def cycle(self) -> None:
        if self.cursor is None:
            # Boot at the newest existing alert: pre-existing rows were either
            # pushed by a previous incarnation or predate the channel entirely.
            async with self.pool.acquire() as conn:
                self.cursor = await conn.fetchval(_NEWEST_TS) or datetime.now(UTC)
            log.info("cursor initialized at %s", self.cursor)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(_NEW_ALERTS, self.cursor, BATCH)
        for row in rows:
            await self.push(row)  # raises on failure -> cursor stays put, retry next cycle
            self.cursor = row["ts"]
            self.pushed += 1
            log.info(
                "pushed alert story=%s ts=%s (total=%d)", row["story_id"], row["ts"], self.pushed
            )


async def run() -> None:
    import asyncpg

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    # Retry, don't crash-exit: Postgres readiness races this pod at install time
    # (same stance as score/api — a CrashLoopBackOff stalls helm --wait).
    pool: asyncpg.Pool | None = None
    while not stop.is_set():
        try:
            pool = await asyncpg.create_pool(os.environ["POSTGRES_DSN"], min_size=1, max_size=2)
            break
        except Exception as e:
            log.warning("postgres not ready (%s: %s); retrying", type(e).__name__, e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=3)
            except TimeoutError:
                pass
    if stop.is_set() or pool is None:
        return

    async with httpx.AsyncClient(timeout=10.0) as client:
        notifier = Notifier(pool, client)
        log.info("notify up: ntfy=%s topic=%s poll=%.0fs", NTFY_URL, NTFY_TOPIC, POLL_S)
        while not stop.is_set():
            try:
                await notifier.cycle()
            except Exception:
                log.exception("notify cycle failed; retrying next interval")
            try:
                await asyncio.wait_for(stop.wait(), timeout=POLL_S)
            except TimeoutError:
                pass
    await pool.close()
    log.info("shutdown: pushed=%d", notifier.pushed)


if __name__ == "__main__":
    asyncio.run(run())
