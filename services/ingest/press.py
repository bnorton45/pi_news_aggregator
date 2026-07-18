"""Shared RSS press-release → Item normalizer for gov agency feeds (PLAN §6.1).

Government press/advisory feeds (State, DoD/war.gov, CISA, CDC, …) are all standard
RSS 2.0 with the same item shape: title / link / guid / pubDate. They differ only in
`source` name and (rarely) trust class, so one pure normalizer serves them all —
each ingester's main.py binds `source`/`source_class` and points the poll runner at it.

Policy (matches the rest of §6.1): keep only the **title** as claim text — the headline
is the primary statement; the CDATA body is boilerplate-laden HTML we deliberately do
not scrape (PLAN §1). NER runs downstream in enrich (§6.3 step 4).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from libs.schema import MAX_TEXT_LEN, Item, SourceClass
from services.ingest.rss import rss_items
from services.ingest.runner import PollSource

# Well-behaved-bot UA (the `Mozilla/5.0 (compatible; …)` convention). Several .gov
# WAFs 200-OK-serve an HTML block page to plain UAs; this is accepted and still
# self-identifies. `rss.rss_items` hard-fails a non-XML body so a block stays loud.
DEFAULT_UA = "Mozilla/5.0 (compatible; osint-aggregator/0.0; +{ident})"


def parse_pubdate(v: Any) -> datetime | None:
    """RFC-822 (`Sat, 11 Jul 2026 13:09:35 +0000`) -> aware datetime, else None.
    Handles 2-digit years and `GMT`/offset zones (CISA/DoD/CDC all differ)."""
    if not isinstance(v, str) or not v:
        return None
    try:
        dt = parsedate_to_datetime(v)
    except (TypeError, ValueError):
        return None
    return dt if dt is not None and dt.tzinfo is not None else None


def dedup_key(item: dict[str, Any]) -> str:
    """guid is the stable per-release id; fall back to link. Press releases are not
    re-issued, so unlike NOAA the key need not track an update field."""
    return str(item.get("guid") or item.get("link") or "")


def rss_to_item(
    item: dict[str, Any],
    *,
    source: str,
    source_class: SourceClass = SourceClass.AUTHORITATIVE,
    max_age_days: int | None = None,
) -> Item | None:
    """Return an Item, or None if the record is unusable / too old. Defensive: feed is
    external input, so never assume a field exists or has the right type.

    `max_age_days` drops items whose pubDate is older than the cutoff — a big backfill
    feed (e.g. CDC's newsroom is ~1800 items) would otherwise re-publish its whole
    history on every pod restart, only for the 5-day wall (§1) to drop it downstream."""
    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    text = title.strip()[:MAX_TEXT_LEN]

    ref = str(item.get("guid") or item.get("link") or "").strip()
    if not ref:
        return None

    ts_event = parse_pubdate(item.get("pubdate"))
    if max_age_days is not None and ts_event is not None:
        if ts_event < datetime.now(UTC) - timedelta(days=max_age_days):
            return None

    link = item.get("link")
    urls = [link[:2_048]] if isinstance(link, str) and link.startswith("http") else []

    return Item(
        source=source,
        source_class=source_class,
        ts_event=ts_event,
        text=text,
        entities=[],  # NER runs downstream in enrich (PLAN §6.3 step 4)
        urls=urls,
        content_hash=ref,  # guid is a stable exact-dedup key
        raw_ref=ref,
    )


def gov_rss_source(
    *,
    name: str,
    feed_url: str,
    source_class: SourceClass = SourceClass.AUTHORITATIVE,
    poll_seconds: float = 300.0,
    max_age_days: int | None = None,
) -> PollSource:
    """Build a hardened poll source for a gov RSS press/advisory feed. Every knob is
    env-overridable (`<NAME>_FEED_URL`, `<NAME>_POLL_SECONDS`, `<NAME>_USER_AGENT`) so
    the image is config-only per deployment. Publishes to `ingest.<name>` alone."""
    env = name.upper()
    ua = os.environ.get(f"{env}_USER_AGENT", DEFAULT_UA.format(ident=f"ingest.{name}"))
    return PollSource(
        name=name,
        subject=os.environ.get("NATS_SUBJECT", f"ingest.{name}"),
        feed_url=os.environ.get(f"{env}_FEED_URL", feed_url),
        poll_seconds=float(os.environ.get(f"{env}_POLL_SECONDS", poll_seconds)),
        extract=lambda items: items or [],
        normalize=lambda it: rss_to_item(
            it, source=name, source_class=source_class, max_age_days=max_age_days
        ),
        dedup_key=dedup_key,
        parse=rss_items,
        headers={"User-Agent": ua},
    )
