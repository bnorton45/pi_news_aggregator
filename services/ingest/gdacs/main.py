"""Hardened GDACS ingester (PLAN §3.1 zone-ingest, §6.1).

Powers: phone home ONLY to www.gdacs.org; NATS *publish only* to ingest.gdacs;
no DB, no other pods. Runs nonroot / read-only-rootfs in a distroless image.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from services.ingest.gdacs.normalize import dedup_key, normalize
from services.ingest.runner import PollSource, run

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


def _extract(payload: Any) -> list[dict[str, Any]]:
    return (payload or {}).get("features", []) or []


SOURCE = PollSource(
    name="gdacs",
    subject=os.environ.get("NATS_SUBJECT", "ingest.gdacs"),
    feed_url=os.environ.get(
        "GDACS_FEED_URL", "https://www.gdacs.org/gdacsapi/api/events/geteventlist/MAP"
    ),
    poll_seconds=float(os.environ.get("GDACS_POLL_SECONDS", "300")),
    extract=_extract,
    normalize=normalize,
    dedup_key=dedup_key,
)

if __name__ == "__main__":
    asyncio.run(run(SOURCE))
