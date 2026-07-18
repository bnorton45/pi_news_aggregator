"""State Dept ingester tests: RSS parsing (shared) + normalize.

Fixtures mirror real https://www.state.gov/rss-feed/press-releases/feed/ shapes,
including the dc:/content: namespaces and the WAF 200-OK-HTML block case.
"""

import httpx
import pytest

from services.ingest.rss import rss_items
from services.ingest.state.normalize import dedup_key, normalize

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
  xmlns:content="http://purl.org/rss/1.0/modules/content/"
  xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>Press Releases &#8211; United States Department of State</title>
    <link>https://www.state.gov/press-releases/</link>
    <item>
      <title>Five Years After the July 11 Demonstrations</title>
      <link>https://www.state.gov/releases/2026/07/five-years-after</link>
      <dc:creator><![CDATA[Marco Rubio, Secretary of State]]></dc:creator>
      <pubDate>Sat, 11 Jul 2026 13:09:35 +0000</pubDate>
      <guid isPermaLink="false">https://www.state.gov/releases/preview/693600/</guid>
      <description><![CDATA[<p>boilerplate</p>]]></description>
      <content:encoded><![CDATA[<p>full body html</p>]]></content:encoded>
    </item>
    <item>
      <title>Second Release</title>
      <link>https://www.state.gov/releases/2026/07/second</link>
      <pubDate>Fri, 10 Jul 2026 09:00:00 +0000</pubDate>
      <guid isPermaLink="false">https://www.state.gov/releases/preview/693599/</guid>
    </item>
  </channel>
</rss>
"""


def _resp(text: str, ctype: str = "application/rss+xml; charset=UTF-8") -> httpx.Response:
    return httpx.Response(200, headers={"content-type": ctype}, text=text)


# ── rss_items (shared parser) ───────────────────────────────────────────────────


def test_rss_items_parses_namespaced_feed() -> None:
    items = rss_items(_resp(FEED))
    assert len(items) == 2
    first = items[0]
    assert first["title"] == "Five Years After the July 11 Demonstrations"
    assert first["link"] == "https://www.state.gov/releases/2026/07/five-years-after"
    assert first["guid"] == "https://www.state.gov/releases/preview/693600/"
    assert first["pubdate"] == "Sat, 11 Jul 2026 13:09:35 +0000"
    assert first["creator"] == "Marco Rubio, Secretary of State"  # dc: namespace flattened


def test_rss_items_rejects_waf_html_block() -> None:
    # www.state.gov returns 200-OK HTML to a non-browser UA; must be a loud error,
    # not a silent empty parse.
    with pytest.raises(ValueError, match="WAF"):
        rss_items(_resp("<html><body>Technical Difficulties</body></html>", ctype="text/html"))


def test_rss_items_malformed_xml_raises() -> None:
    with pytest.raises(Exception):  # noqa: B017 - defusedxml ParseError family
        rss_items(_resp("<rss><channel><item><title>oops", ctype="application/rss+xml"))


# ── normalize ───────────────────────────────────────────────────────────────────


def test_normalize_full_item() -> None:
    item = normalize(rss_items(_resp(FEED))[0])
    assert item is not None
    assert item.source == "state"
    assert item.source_class.value == "authoritative"
    assert item.text == "Five Years After the July 11 Demonstrations"
    assert item.content_hash == "https://www.state.gov/releases/preview/693600/"
    assert item.raw_ref == item.content_hash
    assert item.urls == ["https://www.state.gov/releases/2026/07/five-years-after"]
    assert item.ts_event is not None and item.ts_event.tzinfo is not None
    assert item.entities == []


def test_normalize_missing_title_dropped() -> None:
    assert normalize({"guid": "g", "link": "https://x/1"}) is None
    assert normalize({"title": "   ", "guid": "g"}) is None


def test_normalize_missing_ref_dropped() -> None:
    assert normalize({"title": "A release"}) is None


def test_normalize_bad_pubdate_survives() -> None:
    item = normalize({"title": "A release", "guid": "g1", "pubdate": "not a date"})
    assert item is not None and item.ts_event is None


def test_normalize_non_http_link_gives_no_url() -> None:
    item = normalize({"title": "A release", "guid": "g1", "link": "javascript:evil"})
    assert item is not None and item.urls == []


def test_dedup_key_prefers_guid_then_link() -> None:
    assert dedup_key({"guid": "g1", "link": "l1"}) == "g1"
    assert dedup_key({"link": "l1"}) == "l1"
    assert dedup_key({}) == ""
