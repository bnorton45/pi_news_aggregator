"""Read API (PLAN §6.8): the only thing a user reaches, in zone-present.

FastAPI over the api_ro SELECT-only role. Serves the ranked Story feed, story
detail + velocity sparkline series, alerts, the §6.8 system-health states, and
the static dashboard SPA (vendored assets only — this zone has no internet).

Graceful degradation (§6.8): on a DB failure (cold state-node failover, §2/§4)
list endpoints keep serving their last-known-good payload flagged
`db_degraded: true` instead of erroring out.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from json import loads
from pathlib import Path
from typing import Any
from uuid import UUID

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from services.api.db import STORY_QUERIES, ReadDb

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("api")

# Not "API_PORT": the Service named `api` makes k8s inject API_PORT="tcp://ip:8000"
# (legacy docker-links env) into the pod — int() on that crashed the pod at import.
# The Deployment also sets enableServiceLinks: false; belt and braces.
PORT = int(os.environ.get("API_HTTP_PORT", "8000"))
FEED_WINDOW = timedelta(hours=float(os.environ.get("API_FEED_WINDOW_H", "48")))
SPARK_WINDOW = timedelta(hours=float(os.environ.get("API_SPARK_WINDOW_H", "24")))
SPARK_BIN = timedelta(minutes=float(os.environ.get("API_SPARK_BIN_MIN", "15")))
MAX_LIMIT = 200

db = ReadDb()
_last_good: dict[str, Any] = {}  # per-endpoint last-known-good payload (§6.8 failover)


async def _connect_with_retry() -> None:
    """Postgres readiness races this pod at install time. Serve immediately
    (healthz is DB-free by design; queries answer 503/degraded, §6.8) and keep
    trying — a crash-exit here can stall the whole helm --wait (k3d e2e)."""
    while True:
        try:
            await db.connect()
            log.info("db pool up (api_ro)")
            return
        except Exception as e:
            log.warning("postgres not ready (%s: %s); retrying", type(e).__name__, e)
            await asyncio.sleep(3)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    connector = asyncio.create_task(_connect_with_retry())
    log.info("api up: port=%d feed_window=%s", PORT, FEED_WINDOW)
    yield
    connector.cancel()
    with suppress(asyncio.CancelledError):
        await connector
    await db.close()


app = FastAPI(title="news-aggregator read API", lifespan=_lifespan)


async def _degradable(key: str, fetch) -> dict:
    """Serve fresh data, or the cached last-known-good copy during a DB failover."""
    try:
        data = await fetch()
    except Exception:
        log.exception("DB fetch failed for %s — serving degraded", key)
        if key in _last_good:
            return {**_last_good[key], "db_degraded": True}
        raise HTTPException(status_code=503, detail="DB failover in progress") from None
    payload = {"data": data, "db_degraded": False}
    _last_good[key] = payload
    return payload


def _story_row(r) -> dict:
    return {
        "id": r["id"],
        "first_seen": r["first_seen"],
        "last_seen": r["last_seen"],
        "entity_set": loads(r["entity_set"]),
        "source_set": loads(r["source_set"]),
        "independent_origins": r["independent_origins"],
        "trust_state": r["trust_state"],
        "velocity_z": r["velocity_z"],
        "mainstream_presence": r["mainstream_presence"],
        "inauthenticity": r["inauthenticity"],
        "gap": r["gap"],
        "item_count": r["item_count"] if "item_count" in r.keys() else None,
    }


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}  # liveness only — no DB dependency by design


@app.get("/api/stories")
async def stories(limit: int = 50, sort: str = "gap") -> dict:
    """Feed tab selector (§6.5/§6.8). `gap` (default) = the §6.6 gap-ranked feed;
    `corroborated` / `primary` = trust watchlists (exactly that trust_state), so
    evidence that never reached a high gap stays visible before it ages out; `weather`
    = NOAA/NWS alerts, which every other tab excludes so they don't drown the feed."""
    limit = min(limit, MAX_LIMIT)
    sort = sort if sort in STORY_QUERIES else "gap"

    async def fetch() -> list[dict]:
        return [_story_row(r) for r in await db.stories(FEED_WINDOW, limit, sort)]

    return await _degradable(f"stories:{sort}", fetch)


@app.get("/api/stories/{story_id}")
async def story_detail(story_id: UUID, items: int = 20) -> dict:
    row = await db.story(story_id)
    if row is None:
        raise HTTPException(status_code=404, detail="story not found (or aged out)")
    detail = _story_row(row)
    detail["items"] = [
        {
            "id": i["id"],
            "source": i["source"],
            "source_class": i["source_class"],
            "ts_observed": i["ts_observed"],
            "lang": i["lang"],
            "text": i["text"],
            "urls": loads(i["urls"]),
            "author_ref": i["author_ref"],
            "geo": loads(i["geo"]) if i["geo"] else None,
        }
        for i in await db.story_items(story_id, min(items, MAX_LIMIT))
    ]
    return {"data": detail, "db_degraded": False}


@app.get("/api/stories/{story_id}/velocity")
async def story_velocity(story_id: UUID) -> dict:
    """Sparkline series (§6.8): per-bin firehose mention totals for the story's
    entity set — the same §6.6 signal velocity_z is computed from."""
    row = await db.story(story_id)
    if row is None:
        raise HTTPException(status_code=404, detail="story not found (or aged out)")
    series = await db.velocity_series(loads(row["entity_set"]), SPARK_WINDOW, SPARK_BIN)
    return {
        "data": {
            "bin_minutes": SPARK_BIN.total_seconds() / 60,
            "points": [{"ts": r["bin"], "mentions": r["n"]} for r in series],
        },
        "db_degraded": False,
    }


@app.get("/api/alerts")
async def alerts(limit: int = 50) -> dict:
    async def fetch() -> list[dict]:
        return [
            {
                "story_id": r["story_id"],
                "ts": r["ts"],
                "gap": r["gap"],
                "velocity_z": r["velocity_z"],
                "trust_state": r["trust_state"],
                "entity_set": loads(r["entity_set"]) if r["entity_set"] else [],
            }
            for r in await db.alerts(FEED_WINDOW, min(limit, MAX_LIMIT))
        ]

    return await _degradable("alerts", fetch)


@app.get("/api/health")
async def health() -> dict:
    """§6.8 system-health states: enrich governor beats (sampling active), the
    score beat (baseline warming), assembled from system_state."""

    async def fetch() -> dict:
        rows = await db.system_state()
        states = {r["key"]: {**loads(r["value"]), "updated_at": r["updated_at"]} for r in rows}
        enrich = {k: v for k, v in states.items() if k.startswith("enrich:")}
        return {
            "sampling_active": any(v.get("sampling_active") for v in enrich.values()),
            "baseline_warming": states.get("score", {}).get("baseline_warming", True),
            "components": states,
        }

    return await _degradable("health", fetch)


# Dashboard SPA (§6.8) — mounted last so /api and /healthz win the route match.
_static = Path(__file__).parent / "static"
if _static.is_dir():
    app.mount("/", StaticFiles(directory=_static, html=True), name="dashboard")


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("API_HOST", "0.0.0.0"), port=PORT)  # noqa: S104
