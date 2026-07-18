"""Wikipedia recentchange event -> Item (pure, unit-testable; no network).

Feed: https://stream.wikimedia.org/v2/stream/recentchange (SSE). Each event is a
mediawiki/recentchange/1.0.0 JSON doc: meta{uri,id,domain,dt}, type (edit/new/log/
categorize), namespace, title, comment, user, bot, revision{old,new}, server_name.

The firehose covers every wiki (~tens of events/s); should_keep() shapes it to
article-namespace human edits on an allowlisted domain set BEFORE anything is
published. Wikipedia is PRIMARY (PLAN §6.1): rapid edit activity on a page is an
early breaking-news signal, and edits citing primary documents feed corroboration.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from libs.schema import MAX_TEXT_LEN, Item, SourceClass

SOURCE = "wikipedia"
DEFAULT_DOMAINS = frozenset({"en.wikipedia.org"})
KEEP_TYPES = frozenset({"edit", "new"})


def should_keep(
    evt: dict[str, Any],
    domains: frozenset[str] = DEFAULT_DOMAINS,
    skip_minor: bool = False,
) -> bool:
    """Firehose shaping: allowlisted domain, article namespace, human, edit/new."""
    if not isinstance(evt, dict):
        return False
    meta = evt.get("meta") or {}
    return (
        meta.get("domain") in domains
        and evt.get("namespace") == 0
        and evt.get("type") in KEEP_TYPES
        and evt.get("bot") is False
        and not (skip_minor and evt.get("minor"))
    )


def _hash_author(domain: str, user: Any) -> str:
    """PLAN §6.2: author_ref is a HASHED account id — no raw handle crosses the bus."""
    if not user:
        return ""
    return hashlib.sha256(f"{domain}:{user}".encode()).hexdigest()[:32]


def _parse_dt(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def _lang(server_name: Any) -> str:
    if isinstance(server_name, str) and server_name.endswith(".wikipedia.org"):
        prefix = server_name.removesuffix(".wikipedia.org")
        if 1 < len(prefix) <= 8 and "." not in prefix:
            return prefix
    return "und"


def normalize(evt: dict[str, Any]) -> Item | None:
    """Return an Item, or None if the event is unusable. Defensive: the stream is
    external input, so never assume a field exists or has the right type."""
    meta = evt.get("meta") or {}
    domain = meta.get("domain")
    title = evt.get("title")
    if not isinstance(domain, str) or not title:
        return None

    comment = evt.get("comment")
    text = f"{title} — {comment}" if isinstance(comment, str) and comment else str(title)
    text = text[:MAX_TEXT_LEN]

    urls = []
    uri = meta.get("uri") or evt.get("title_url")
    if isinstance(uri, str) and uri.startswith("http"):
        urls.append(uri[:2_048])

    rev = (evt.get("revision") or {}).get("new")
    if rev:
        key = f"wiki:{domain}:{rev}"[:256]
    elif meta.get("id"):
        key = f"wiki:{domain}:{meta['id']}"[:256]
    else:
        return None

    return Item(
        source=SOURCE,
        source_class=SourceClass.PRIMARY,
        ts_event=_parse_dt(meta.get("dt")),
        lang=_lang(evt.get("server_name")),
        text=text,
        urls=urls,
        author_ref=_hash_author(domain, evt.get("user")),
        content_hash=key,  # revision id is the stable exact-dedup key
        raw_ref=str(meta.get("id", ""))[:256],
    )
