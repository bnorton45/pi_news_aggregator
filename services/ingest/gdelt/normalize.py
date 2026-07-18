"""GDELT DOC 2.0 ArtList article -> Item (pure, unit-testable; no network).

Feed: https://api.gdeltproject.org/api/v2/doc/doc?mode=artlist&format=json
Each article: {url, title, seendate ("20260703T234500Z"), domain, language,
sourcecountry}. This is the MAINSTREAM baseline (PLAN §6.1): headline/metadata
presence ONLY — never content-scraped. Google News RSS was considered and
rejected (its ToS limits the feed to personal feed readers); Reuters/AP have no
public RSS anymore. GDELT is the index the PLAN already earmarked for this.

author_ref carries the outlet domain in plaintext: it is an organization, not a
person (§6.2's hashing protects account ids), and §6.6/§6.7 need the domain for
mainstream-presence and source-reputation math.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from libs.schema import MAX_TEXT_LEN, Item, SourceClass

SOURCE = "gdelt"


def _parse_seendate(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def dedup_key(article: dict[str, Any]) -> str:
    return str(article.get("url", ""))


def normalize(article: dict[str, Any]) -> Item | None:
    """Return an Item, or None if the article is unusable. Defensive: the index is
    external input, so never assume a field exists or has the right type."""
    url = article.get("url")
    title = article.get("title")
    if not isinstance(url, str) or not url.startswith("http") or not title:
        return None

    lang = "en" if str(article.get("language", "")).lower() == "english" else "und"

    return Item(
        source=SOURCE,
        source_class=SourceClass.MAINSTREAM,
        ts_event=_parse_seendate(article.get("seendate")),
        lang=lang,
        text=str(title)[:MAX_TEXT_LEN],  # headline ONLY — presence, not content
        urls=[url[:2_048]],
        author_ref=str(article.get("domain", ""))[:256],  # outlet, not a person
        content_hash=f"gdelt:{hashlib.sha256(url.encode()).hexdigest()[:40]}",
        raw_ref=str(article.get("domain", ""))[:256],
    )
