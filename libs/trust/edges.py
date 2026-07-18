"""Provenance-edge detection (PLAN §6.5) — pure, DB-free, unit-testable.

A dependency edge between two Items means one is NOT an independent origin of
the other. Edge types, per §6.5:

  (a) REF    — one references/quotes/reposts/replies to the other: an item's
               `parent_ref` equals the other's `content_hash` (the ingesters
               deliberately key both with the same hash function, §6.2).
  (b) COPY   — near-identical text (copypasta): simhash Hamming distance below
               threshold. Compared pairwise WITHIN a Story only — bounded by
               story size, so this stays sub-linear at firehose scale without
               the global LSH index (§6.3 step 2 builds that later for dedup).
  (c) URL    — both carry the same canonical upstream URL.
  (d) AUTHOR — same `author_ref` (same account, or same outlet domain for
               mainstream items — which is what realizes §6.5's "deduped by
               distinct org/domain" inside the component count).

All string fields originate from untrusted feeds: treat them as opaque bytes,
never parse beyond the minimal canonicalization here.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import UUID

from libs.dedup.simhash import hamming64

# Copypasta threshold: ≤6 differing bits of 64 ≈ small edits on identical text.
COPY_HAMMING_MAX = 6

# Tracking params stripped during URL canonicalization (never identity-bearing).
_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_", "ref_")


class EdgeType(str, enum.Enum):
    REF = "ref"
    COPY = "copy"
    URL = "url"
    AUTHOR = "author"


# Classes whose items collapse by wire attribution (§6.5): outlets that CARRY wire
# copy. Social stays out — a social account quoting AP is that account's own act of
# amplification (COPY edges still collapse verbatim reposts), and authoritative/
# primary sources never run wire copy.
_WIRE_COLLAPSE_CLASSES = frozenset({"local_news", "mainstream"})


@dataclass(frozen=True)
class ProvNode:
    """The provenance-relevant projection of a stored Item."""

    item_id: UUID
    author_ref: str
    parent_ref: str | None
    content_hash: str
    urls: frozenset[str]  # already canonicalized via canonical_url()
    simhash: int  # unsigned 64-bit; 0 = no usable text
    source: str = ""  # feed name; org fallback for the origin key
    source_class: str = ""  # 'social' | 'authoritative' | 'primary' | 'mainstream' | 'local_news'
    wire_ref: str = ""  # credited wire service ('ap', …) via libs.trust.wire; '' = none

    @property
    def origin_key(self) -> str:
        """Org/domain identity for §6.5's origin dedup. Wire attribution takes
        precedence for outlet classes: N stations carrying one AP story are ONE
        reporting origin (AP) no matter how much each localized the copy — that is
        what keeps mass syndication from forging corroboration. Otherwise
        author_ref when present (account, or outlet domain for gdelt); for
        non-social classes the feed itself is one organization (USGS, NOAA…), so
        `source` stands in when the ingester sets no author — N items from one
        authority are ONE origin. Anonymous social items get no key: they never
        merge by authorship."""
        if self.wire_ref and self.source_class in _WIRE_COLLAPSE_CLASSES:
            return f"wire:{self.wire_ref}"
        if self.author_ref:
            return self.author_ref
        if self.source_class and self.source_class != "social":
            return f"src:{self.source}"
        return ""


@dataclass(frozen=True)
class ProvEdge:
    src: UUID
    dst: UUID
    edge_type: EdgeType


def canonical_url(url: str) -> str:
    """Canonical form for edge (c): lowercase host, no scheme/www/fragment,
    tracking params dropped, trailing slash trimmed. Unparseable input → ''."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    host = parts.netloc.casefold().removeprefix("www.")
    if not host:
        return ""
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.casefold().startswith(_TRACKING_PREFIXES)
        ]
    )
    path = parts.path.rstrip("/")
    return f"{host}{path}" + (f"?{query}" if query else "")


def detect_edges(new: ProvNode, members: list[ProvNode]) -> list[ProvEdge]:
    """Edges from `new` to each existing Story member it depends on (or vice
    versa). At most one edge per (pair, type); pairs may carry several types."""
    out: list[ProvEdge] = []
    for m in members:
        if m.item_id == new.item_id:
            continue
        if (new.parent_ref and new.parent_ref == m.content_hash) or (
            m.parent_ref and m.parent_ref == new.content_hash
        ):
            out.append(ProvEdge(new.item_id, m.item_id, EdgeType.REF))
        if new.simhash and m.simhash and hamming64(new.simhash, m.simhash) <= COPY_HAMMING_MAX:
            out.append(ProvEdge(new.item_id, m.item_id, EdgeType.COPY))
        if new.urls & m.urls:
            out.append(ProvEdge(new.item_id, m.item_id, EdgeType.URL))
        if new.author_ref and new.author_ref == m.author_ref:
            out.append(ProvEdge(new.item_id, m.item_id, EdgeType.AUTHOR))
    return out
