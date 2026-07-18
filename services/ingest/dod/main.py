"""Hardened U.S. Dept of Defense news-release ingester (PLAN §3.1 zone-ingest, §6.1).

Powers: phone home ONLY to www.war.gov; NATS *publish only* to ingest.dod; no DB, no
other pods. Runs nonroot / read-only-rootfs in a distroless image.

Feed: defense.gov redirects to **war.gov** (the department was rebranded "Department of
War"); the DNN ArticleCS RSS is standard RSS 2.0. AUTHORITATIVE class → feeds
PRIMARY_BACKED. Override the feed/UA/poll via DOD_FEED_URL / DOD_USER_AGENT /
DOD_POLL_SECONDS.
"""

from __future__ import annotations

import asyncio
import logging
import os

from services.ingest.press import gov_rss_source
from services.ingest.runner import run

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

SOURCE = gov_rss_source(
    name="dod",
    feed_url="https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?max=10&ContentType=1&Site=945",
)

if __name__ == "__main__":
    asyncio.run(run(SOURCE))
