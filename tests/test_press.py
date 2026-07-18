"""Shared gov-RSS normalizer + source-builder tests (services.ingest.press)."""

from datetime import UTC, datetime, timedelta

from libs.schema import SourceClass
from services.ingest.press import dedup_key, gov_rss_source, rss_to_item

RECENT = (datetime.now(UTC) - timedelta(hours=2)).strftime("%a, %d %b %Y %H:%M:%S %z")
OLD = (datetime.now(UTC) - timedelta(days=30)).strftime("%a, %d %b %Y %H:%M:%S %z")

ITEM = {
    "title": "Secretary Statement on the Situation",
    "link": "https://www.war.gov/News/Article/4540150/x",
    "guid": "https://www.war.gov/News/Article/4540150/x",
    "pubdate": "Fri, 10 Jul 2026 16:08:00 GMT",
}


# ── rss_to_item ─────────────────────────────────────────────────────────────────


def test_rss_to_item_full() -> None:
    item = rss_to_item(ITEM, source="dod")
    assert item is not None
    assert item.source == "dod"
    assert item.source_class is SourceClass.AUTHORITATIVE
    assert item.text == "Secretary Statement on the Situation"
    assert item.content_hash == ITEM["guid"] and item.raw_ref == ITEM["guid"]
    assert item.urls == [ITEM["link"]]
    assert item.ts_event is not None and item.ts_event.tzinfo is not None
    assert item.entities == []


def test_two_digit_year_and_zone_variants() -> None:
    # CISA uses `... 26 12:00:00 +0000`; must still parse to an aware datetime.
    it = {**ITEM, "pubdate": "Fri, 10 Jul 26 12:00:00 +0000"}
    out = rss_to_item(it, source="cisa")
    assert out is not None and out.ts_event is not None and out.ts_event.year == 2026


def test_missing_title_or_ref_dropped() -> None:
    assert rss_to_item({"guid": "g"}, source="dod") is None
    assert rss_to_item({"title": "  "}, source="dod") is None
    assert rss_to_item({"title": "A release"}, source="dod") is None  # no guid/link


def test_relative_guid_kept_non_http_link_no_url() -> None:
    # CISA guid is `/node/25143`; still a valid stable ref, but not a URL.
    it = {"title": "Advisory", "guid": "/node/25143", "link": "/node/25143"}
    out = rss_to_item(it, source="cisa")
    assert out is not None
    assert out.content_hash == "/node/25143"
    assert out.urls == []


def test_bad_pubdate_survives() -> None:
    out = rss_to_item({"title": "x", "guid": "g", "pubdate": "nonsense"}, source="dod")
    assert out is not None and out.ts_event is None


def test_max_age_drops_old_keeps_recent() -> None:
    def at(pubdate: str | None) -> object:
        it = {"title": "x", "guid": "g"} | ({"pubdate": pubdate} if pubdate else {})
        return rss_to_item(it, source="cdc", max_age_days=7)

    assert at(OLD) is None
    assert at(RECENT) is not None
    assert at(None) is not None  # undated item kept — can't judge age
    # No cutoff => even an old item is kept.
    assert rss_to_item({"title": "x", "guid": "g", "pubdate": OLD}, source="cdc") is not None


def test_dedup_key() -> None:
    assert dedup_key({"guid": "g1", "link": "l1"}) == "g1"
    assert dedup_key({"link": "l1"}) == "l1"
    assert dedup_key({}) == ""


# ── gov_rss_source builder ──────────────────────────────────────────────────────


def test_gov_rss_source_wires_each_feed() -> None:
    for name, host in [("dod", "war.gov"), ("cisa", "cisa.gov"), ("cdc", "tools.cdc.gov")]:
        src = gov_rss_source(name=name, feed_url=f"https://{host}/feed.xml")
        assert src.name == name
        assert src.subject == f"ingest.{name}"
        assert host in src.feed_url
        assert src.headers["User-Agent"].startswith("Mozilla/5.0 (compatible;")
        # normalize is bound to the right source/class
        out = src.normalize(ITEM)
        assert out is not None and out.source == name


def test_gov_rss_source_env_override(monkeypatch) -> None:
    monkeypatch.setenv("DOD_FEED_URL", "https://example.gov/custom.xml")
    monkeypatch.setenv("DOD_USER_AGENT", "custom-ua")
    src = gov_rss_source(name="dod", feed_url="https://www.war.gov/default.xml")
    assert src.feed_url == "https://example.gov/custom.xml"
    assert src.headers["User-Agent"] == "custom-ua"
