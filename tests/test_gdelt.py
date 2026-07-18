"""GDELT ArtList normalize tests — fixtures mirror the DOC 2.0 JSON shapes."""

from datetime import UTC

from services.ingest.gdelt.normalize import dedup_key, normalize

ARTICLE = {
    "url": "https://www.example-outlet.com/news/quake-guatemala-2026",
    "url_mobile": "",
    "title": "Strong earthquake shakes Guatemala City",
    "seendate": "20260703T234500Z",
    "socialimage": "",
    "domain": "example-outlet.com",
    "language": "English",
    "sourcecountry": "United States",
}


def test_normalize_article() -> None:
    item = normalize(ARTICLE)
    assert item is not None
    assert item.source == "gdelt"
    assert item.source_class.value == "mainstream"
    assert item.text == "Strong earthquake shakes Guatemala City"
    assert item.lang == "en"
    assert item.urls == ["https://www.example-outlet.com/news/quake-guatemala-2026"]
    assert item.author_ref == "example-outlet.com"
    assert item.content_hash.startswith("gdelt:")


def test_presence_only_no_body() -> None:
    """MAINSTREAM is headline/metadata presence — no body field must ever leak in."""
    item = normalize({**ARTICLE, "body": "FULL ARTICLE TEXT THAT MUST NOT BE INGESTED"})
    assert item is not None
    assert item.text == ARTICLE["title"]


def test_seendate_parses_utc() -> None:
    item = normalize(ARTICLE)
    assert item is not None
    assert item.ts_event is not None and item.ts_event.tzinfo is UTC
    assert item.ts_event.year == 2026 and item.ts_event.hour == 23


def test_non_english_lang_und() -> None:
    item = normalize({**ARTICLE, "language": "Spanish"})
    assert item is not None
    assert item.lang == "und"


def test_dedup_key_is_url() -> None:
    assert dedup_key(ARTICLE) == ARTICLE["url"]


def test_unusable_article_dropped() -> None:
    assert normalize({}) is None
    assert normalize({"url": "notaurl", "title": "x"}) is None
    assert normalize({"url": "https://x.example", "title": ""}) is None
