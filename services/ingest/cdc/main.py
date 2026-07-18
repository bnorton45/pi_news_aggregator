"""Hardened CDC newsroom ingester (PLAN §3.1 zone-ingest, §6.1).

Powers: phone home ONLY to tools.cdc.gov; NATS *publish only* to ingest.cdc; no DB, no
other pods. Runs nonroot / read-only-rootfs in a distroless image.

Feed: https://tools.cdc.gov/api/v2/resources/media/132608.rss — the CDC Online Newsroom
(standard RSS 2.0). It carries the FULL history (~1800 items), so `max_age_days` bounds
what we publish to recent items — otherwise every pod restart would re-emit the whole
backfill only for the 5-day wall (§1) to drop it downstream. AUTHORITATIVE class → feeds
PRIMARY_BACKED. Override via CDC_FEED_URL / CDC_USER_AGENT / CDC_POLL_SECONDS.
"""

from __future__ import annotations

import asyncio
import logging
import os

from services.ingest.press import gov_rss_source
from services.ingest.runner import run

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

SOURCE = gov_rss_source(
    name="cdc",
    feed_url="https://tools.cdc.gov/api/v2/resources/media/132608.rss",
    max_age_days=int(os.environ.get("CDC_MAX_AGE_DAYS", "7")),
)

if __name__ == "__main__":
    asyncio.run(run(SOURCE))
