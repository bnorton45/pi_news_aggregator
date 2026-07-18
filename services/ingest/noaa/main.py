"""Hardened NOAA/NWS ingester (PLAN §3.1 zone-ingest, §6.1).

Powers: phone home ONLY to api.weather.gov; NATS *publish only* to ingest.noaa;
no DB, no other pods. Runs nonroot / read-only-rootfs in a distroless image.

api.weather.gov asks callers for an identifying User-Agent; set NOAA_USER_AGENT
(e.g. with an operator contact) to override the generic default.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from services.ingest.noaa.normalize import dedup_key, normalize
from services.ingest.runner import PollSource, run

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


def _extract(payload: Any) -> list[dict[str, Any]]:
    return (payload or {}).get("features", []) or []


_headers = {"Accept": "application/geo+json"}
if os.environ.get("NOAA_USER_AGENT"):
    _headers["User-Agent"] = os.environ["NOAA_USER_AGENT"]

SOURCE = PollSource(
    name="noaa",
    subject=os.environ.get("NATS_SUBJECT", "ingest.noaa"),
    feed_url=os.environ.get("NOAA_FEED_URL", "https://api.weather.gov/alerts/active"),
    poll_seconds=float(os.environ.get("NOAA_POLL_SECONDS", "120")),
    extract=_extract,
    normalize=normalize,
    dedup_key=dedup_key,
    headers=_headers,
)

if __name__ == "__main__":
    asyncio.run(run(SOURCE))
