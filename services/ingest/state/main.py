"""Hardened U.S. Dept of State press-release ingester (PLAN §3.1 zone-ingest, §6.1).

Powers: phone home ONLY to www.state.gov; NATS *publish only* to ingest.state;
no DB, no other pods. Runs nonroot / read-only-rootfs in a distroless image.

WAF note: www.state.gov returns 200-OK HTML (not a 4xx) to non-browser
User-Agents, so a plain UA silently yields zero items. A `Mozilla/5.0
(compatible; …)` UA — the long-standing well-behaved-bot convention — is accepted
and still self-identifies as this aggregator. Override via STATE_USER_AGENT to add
an operator contact. `rss.rss_items` also hard-fails on a non-XML body so a WAF
block is a loud poll error, never a silent no-op.
"""

from __future__ import annotations

import asyncio
import logging
import os

from services.ingest.rss import rss_items
from services.ingest.runner import PollSource, run
from services.ingest.state.normalize import dedup_key, normalize

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

_DEFAULT_UA = "Mozilla/5.0 (compatible; osint-aggregator/0.0; +ingest.state)"


def _extract(items: list[dict]) -> list[dict]:
    return items or []


SOURCE = PollSource(
    name="state",
    subject=os.environ.get("NATS_SUBJECT", "ingest.state"),
    feed_url=os.environ.get(
        "STATE_FEED_URL", "https://www.state.gov/rss-feed/press-releases/feed/"
    ),
    poll_seconds=float(os.environ.get("STATE_POLL_SECONDS", "300")),
    extract=_extract,
    normalize=normalize,
    dedup_key=dedup_key,
    parse=rss_items,
    headers={"User-Agent": os.environ.get("STATE_USER_AGENT", _DEFAULT_UA)},
)

if __name__ == "__main__":
    asyncio.run(run(SOURCE))
