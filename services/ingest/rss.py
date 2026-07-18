"""Safe RSS/Atom parsing for pull-based ingesters (PLAN §6.1).

Feeds are untrusted internet input, so XML is parsed with `defusedxml` (blocks
entity-expansion / external-entity / billion-laughs attacks that stdlib ElementTree
is vulnerable to). Returns one flat dict per <item>, keyed by the local tag name
(namespace stripped) so callers need not know about `dc:`/`content:` prefixes.

Gotcha this guards against: several .gov feeds sit behind a WAF that returns
`200 OK` with an HTML "access denied" body — not a 4xx — to any request whose
User-Agent isn't browser-shaped. That sails past `raise_for_status()` and would
silently yield zero items forever. `rss_items` rejects a non-XML content-type up
front with a clear error so the failure is visible (fix = a browser-shaped UA;
`Mozilla/5.0 (compatible; …)` is enough — see the state ingester).
"""

from __future__ import annotations

from typing import Any

import httpx
from defusedxml.ElementTree import fromstring

# Item children we surface; CDATA is returned as-is (normalize decides what to keep).
_WANTED = frozenset({"title", "link", "guid", "pubdate", "creator", "description"})


def _local(tag: str) -> str:
    """Strip the `{namespace}` prefix ElementTree prepends → local tag name."""
    return tag.rsplit("}", 1)[-1].lower()


def rss_items(response: httpx.Response) -> list[dict[str, str]]:
    """Parse an RSS/Atom response into a list of item dicts (namespace-flattened).

    Raises ValueError on a non-XML body (the WAF-block case above) and lets
    defusedxml raise on malformed XML — both surface as a visible poll failure."""
    ctype = response.headers.get("content-type", "").lower()
    if "xml" not in ctype and "rss" not in ctype:
        raise ValueError(
            f"expected an XML/RSS body, got content-type {ctype!r} "
            f"({len(response.content)} bytes) — likely a WAF block; check the User-Agent"
        )

    root = fromstring(response.text)
    items: list[dict[str, str]] = []
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        fields: dict[str, str] = {}
        for child in item:
            name = _local(child.tag)
            if name in _WANTED and name not in fields:  # first wins (e.g. primary <link>)
                fields[name] = (child.text or "").strip()
        items.append(fields)
    return items


def json_payload(response: httpx.Response) -> Any:
    """Default parse for JSON feeds (USGS/NOAA/GDACS): the decoded body."""
    return response.json()
