"""Hardened GDELT ingester (PLAN §3.1 zone-ingest, §6.1 mainstream baseline).

Powers: phone home ONLY to api.gdeltproject.org; NATS *publish only* to
ingest.gdelt; no DB, no other pods. Runs nonroot / read-only-rootfs.

GDELT asks for at most one request per 5 seconds; the default 120s poll is far
under that, and the seen-LRU collapses the overlap between polls.

Live-verified 2026-07-04: the documented `timespan` parameter is broken server-
side — 15min/30min get a plaintext "Timespan is too short." (HTTP 200), and any
accepted value (60min, 1d) returns zero results in every format. The working
shape is NO timespan + `sort=datedesc`, which returns the newest `maxrecords`
indexed articles; overlap dedup is the seen-LRU's job anyway. GDELT_TIMESPAN is
kept as an opt-in env for if/when the parameter is fixed upstream.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from urllib.parse import quote

from services.ingest.gdelt.normalize import dedup_key, normalize
from services.ingest.runner import PollSource, run

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

QUERY = os.environ.get("GDELT_QUERY", "sourcelang:english")
TIMESPAN = os.environ.get("GDELT_TIMESPAN", "")  # broken upstream; see docstring
MAX_RECORDS = int(os.environ.get("GDELT_MAX_RECORDS", "250"))

FEED_URL = os.environ.get(
    "GDELT_FEED_URL",
    "https://api.gdeltproject.org/api/v2/doc/doc"
    f"?query={quote(QUERY)}&mode=artlist&format=json"
    f"&maxrecords={MAX_RECORDS}&sort=datedesc" + (f"&timespan={TIMESPAN}" if TIMESPAN else ""),
)


def _extract(payload: Any) -> list[dict[str, Any]]:
    return (payload or {}).get("articles", []) or []


SOURCE = PollSource(
    name="gdelt",
    subject=os.environ.get("NATS_SUBJECT", "ingest.gdelt"),
    feed_url=FEED_URL,
    poll_seconds=float(os.environ.get("GDELT_POLL_SECONDS", "120")),
    extract=_extract,
    normalize=normalize,
    dedup_key=dedup_key,
)

if __name__ == "__main__":
    asyncio.run(run(SOURCE))
