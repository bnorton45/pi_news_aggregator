"""Bluesky Jetstream commit event -> Item (pure, unit-testable; no network).

Feed: wss://jetstream*.bsky.network/subscribe?wantedCollections=app.bsky.feed.post
Each event: {did, time_us, kind:"commit", commit:{operation, collection, rkey, cid,
record:{$type, createdAt, langs, text, reply{parent{uri,cid}}, facets[]}}}.

Bluesky is SOCIAL (PLAN §6.1) — high noise, requires corroboration. Identity policy
(§6.2): the at-uri and did are raw account identifiers, so content_hash/parent_ref/
author_ref are all sha256-derived — dependency edges still link (same function on
both ends) but no raw handle crosses the bus.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from libs.schema import MAX_TEXT_LEN, MAX_URLS, Item, SourceClass

SOURCE = "bluesky"
DEFAULT_LANGS = frozenset({"en"})
POST_COLLECTION = "app.bsky.feed.post"


def _uri_key(uri: str) -> str:
    return f"bsky:{hashlib.sha256(uri.encode()).hexdigest()[:40]}"


def should_keep(evt: dict[str, Any], langs: frozenset[str] = DEFAULT_LANGS) -> bool:
    """Firehose shaping: created posts in an allowlisted language, non-empty text."""
    if not isinstance(evt, dict) or evt.get("kind") != "commit":
        return False
    commit = evt.get("commit") or {}
    if commit.get("operation") != "create" or commit.get("collection") != POST_COLLECTION:
        return False
    record = commit.get("record") or {}
    text = record.get("text")
    if not isinstance(text, str) or not text.strip():
        return False
    post_langs = record.get("langs")
    if not isinstance(post_langs, list):
        return False  # unlabeled posts are untriageable at firehose rate
    return any(str(lang).lower().split("-")[0] in langs for lang in post_langs)


def _parse_dt(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def _facet_urls(record: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for facet in record.get("facets") or []:
        if not isinstance(facet, dict):
            continue
        for feat in facet.get("features") or []:
            uri = feat.get("uri") if isinstance(feat, dict) else None
            if isinstance(uri, str) and uri.startswith("http") and uri not in urls:
                urls.append(uri[:2_048])
            if len(urls) >= MAX_URLS:
                return urls
    return urls


def normalize(evt: dict[str, Any]) -> Item | None:
    """Return an Item, or None if the event is unusable. Defensive: the firehose is
    external input, so never assume a field exists or has the right type."""
    did = evt.get("did")
    commit = evt.get("commit") or {}
    record = commit.get("record") or {}
    rkey = commit.get("rkey")
    text = record.get("text")
    if not isinstance(did, str) or not rkey or not isinstance(text, str) or not text.strip():
        return None

    at_uri = f"at://{did}/{POST_COLLECTION}/{rkey}"

    parent_ref = None
    parent_uri = ((record.get("reply") or {}).get("parent") or {}).get("uri")
    if isinstance(parent_uri, str) and parent_uri:
        parent_ref = _uri_key(parent_uri)

    langs = record.get("langs")
    lang = "und"
    if isinstance(langs, list) and langs and isinstance(langs[0], str):
        lang = langs[0].lower()[:8]

    return Item(
        source=SOURCE,
        source_class=SourceClass.SOCIAL,
        ts_event=_parse_dt(record.get("createdAt")),
        lang=lang,
        text=text[:MAX_TEXT_LEN],
        urls=_facet_urls(record),
        author_ref=hashlib.sha256(did.encode()).hexdigest()[:32],
        parent_ref=parent_ref,
        content_hash=_uri_key(at_uri),  # post identity; matches parent_ref linkage
        raw_ref=str(commit.get("cid", ""))[:256],
    )
