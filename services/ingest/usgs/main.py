"""Hardened USGS ingester (PLAN §3.1 zone-ingest, §6.1).

Powers: phone home ONLY to earthquake.usgs.gov; NATS *publish only* to ingest.usgs;
no DB, no other pods. Runs nonroot / read-only-rootfs in a distroless image.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from services.ingest.runner import PollSource, run
from services.ingest.usgs.normalize import dedup_key, normalize

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


def _extract(payload: Any) -> list[dict[str, Any]]:
    return (payload or {}).get("features", []) or []


SOURCE = PollSource(
    name="usgs",
    subject=os.environ.get("NATS_SUBJECT", "ingest.usgs"),
    feed_url=os.environ.get(
        "USGS_FEED_URL",
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson",
    ),
    poll_seconds=float(os.environ.get("USGS_POLL_SECONDS", "60")),
    extract=_extract,
    normalize=normalize,
    dedup_key=dedup_key,
)

if __name__ == "__main__":
    asyncio.run(run(SOURCE))
