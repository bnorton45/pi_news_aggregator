"""Mastodon status JSON -> Item (pure, unit-testable; no network).

Feed: GET /api/v1/timelines/public?since_id=... on a *selected instance set*
(PLAN §6.1). mastodon.social now requires auth for this endpoint; the default set
is open instances. Status carries {id, created_at, in_reply_to_id, language, uri,
url, visibility, content (HTML), account{id, bot}, reblog}.

Mastodon is SOCIAL — high noise, requires corroboration. Identity policy (§6.2):
author_ref is sha256-derived; content_hash keys on the global ActivityPub uri, so
the same federated status seen from two instances collapses in the shared KV dedup.
parent_ref is deliberately unset for now: replies only carry the instance-local
in_reply_to_id (not the parent's uri), so a consistent cross-item key isn't
derivable cheaply — §6.5's simhash/canonical-URL edges cover mastodon provenance.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime
from typing import Any

from libs.schema import MAX_TEXT_LEN, Item, SourceClass

SOURCE = "mastodon"
DEFAULT_LANGS = frozenset({"en"})

_BREAKS = re.compile(r"<(?:br\s*/?|/p)>", re.I)
_TAGS = re.compile(r"<[^>]+>")


def strip_html(content: str) -> str:
    """Status content is HTML; flatten to text with paragraph/line breaks kept."""
    text = _BREAKS.sub("\n", content)
    text = _TAGS.sub("", text)
    return html.unescape(text).strip()


def should_keep(
    status: dict[str, Any],
    langs: frozenset[str] = DEFAULT_LANGS,
    skip_bots: bool = True,
) -> bool:
    """Timeline shaping: public original posts (no boosts) from an allowlisted
    language; bot accounts dropped by default."""
    if not isinstance(status, dict):
        return False
    if status.get("reblog") is not None:
        return False  # boosts are duplicates; the original arrives on its own
    if status.get("visibility") != "public":
        return False
    lang = status.get("language")
    if not isinstance(lang, str) or lang.lower().split("-")[0] not in langs:
        return False
    if skip_bots and (status.get("account") or {}).get("bot"):
        return False
    content = status.get("content")
    return isinstance(content, str) and bool(strip_html(content))


def _parse_dt(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def normalize(status: dict[str, Any], instance: str) -> Item | None:
    """Return an Item, or None if the status is unusable. Defensive: the timeline
    is external input, so never assume a field exists or has the right type."""
    uri = status.get("uri")
    content = status.get("content")
    if not isinstance(uri, str) or not uri or not isinstance(content, str):
        return None
    text = strip_html(content)
    if not text:
        return None

    account_id = (status.get("account") or {}).get("id")
    author_ref = ""
    if account_id:
        author_ref = hashlib.sha256(f"{instance}:{account_id}".encode()).hexdigest()[:32]

    urls = []
    url = status.get("url")
    if isinstance(url, str) and url.startswith("http"):
        urls.append(url[:2_048])

    lang = status.get("language")
    lang = lang.lower()[:8] if isinstance(lang, str) and lang else "und"

    return Item(
        source=SOURCE,
        source_class=SourceClass.SOCIAL,
        ts_event=_parse_dt(status.get("created_at")),
        lang=lang,
        text=text[:MAX_TEXT_LEN],
        urls=urls,
        author_ref=author_ref,
        content_hash=f"masto:{hashlib.sha256(uri.encode()).hexdigest()[:40]}",
        raw_ref=str(status.get("id", ""))[:256],
    )
