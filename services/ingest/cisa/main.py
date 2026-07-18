"""Hardened CISA cybersecurity-advisories ingester (PLAN §3.1 zone-ingest, §6.1).

Powers: phone home ONLY to www.cisa.gov; NATS *publish only* to ingest.cisa; no DB, no
other pods. Runs nonroot / read-only-rootfs in a distroless image.

Feed: https://www.cisa.gov/cybersecurity-advisories/all.xml — standard RSS 2.0 of alerts
and advisories (guid is a relative /node/<id>, which is fine as a stable dedup key).
AUTHORITATIVE class → feeds PRIMARY_BACKED. Override via CISA_FEED_URL / CISA_USER_AGENT /
CISA_POLL_SECONDS.
"""

from __future__ import annotations

import asyncio
import logging
import os

from services.ingest.press import gov_rss_source
from services.ingest.runner import run

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

SOURCE = gov_rss_source(
    name="cisa",
    feed_url="https://www.cisa.gov/cybersecurity-advisories/all.xml",
)

if __name__ == "__main__":
    asyncio.run(run(SOURCE))
