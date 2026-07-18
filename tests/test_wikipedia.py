"""Wikipedia recentchange normalize/should_keep tests — fixtures mirror the
mediawiki/recentchange/1.0.0 SSE payloads."""

from services.ingest.wikipedia.normalize import _hash_author, normalize, should_keep

EDIT = {
    "$schema": "/mediawiki/recentchange/1.0.0",
    "meta": {
        "uri": "https://en.wikipedia.org/wiki/Earthquake",
        "id": "34243304-6e65-4af3-ad50-58531688e895",
        "domain": "en.wikipedia.org",
        "dt": "2026-07-03T23:37:13.999Z",
    },
    "id": 86972341,
    "type": "edit",
    "namespace": 0,
    "title": "Earthquake",
    "comment": "add USGS reference for the 2026 event",
    "user": "SomeEditor",
    "bot": False,
    "minor": False,
    "revision": {"old": 17904631, "new": 18692530},
    "server_name": "en.wikipedia.org",
}


def test_keep_article_human_edit() -> None:
    assert should_keep(EDIT) is True


def test_shaping_drops_noise() -> None:
    assert should_keep({**EDIT, "bot": True}) is False
    assert should_keep({**EDIT, "namespace": 1}) is False
    assert should_keep({**EDIT, "type": "categorize"}) is False
    assert should_keep({**EDIT, "type": "log"}) is False
    assert should_keep({**EDIT, "meta": {**EDIT["meta"], "domain": "ce.wikipedia.org"}}) is False
    assert should_keep({**EDIT, "minor": True}, skip_minor=True) is False
    assert should_keep({**EDIT, "minor": True}, skip_minor=False) is True


def test_normalize_edit() -> None:
    item = normalize(EDIT)
    assert item is not None
    assert item.source == "wikipedia"
    assert item.source_class.value == "primary"
    assert item.text == "Earthquake — add USGS reference for the 2026 event"
    assert item.lang == "en"
    assert item.urls == ["https://en.wikipedia.org/wiki/Earthquake"]
    assert item.content_hash == "wiki:en.wikipedia.org:18692530"
    assert item.ts_event is not None and item.ts_event.tzinfo is not None


def test_author_is_hashed_not_raw() -> None:
    item = normalize(EDIT)
    assert item is not None
    assert item.author_ref != "" and "SomeEditor" not in item.author_ref
    assert item.author_ref == _hash_author("en.wikipedia.org", "SomeEditor")


def test_new_page_without_comment() -> None:
    evt = {**EDIT, "type": "new", "comment": ""}
    item = normalize(evt)
    assert item is not None
    assert item.text == "Earthquake"


def test_meta_id_fallback_when_no_revision() -> None:
    evt = {**EDIT, "revision": {}}
    item = normalize(evt)
    assert item is not None
    assert item.content_hash == "wiki:en.wikipedia.org:34243304-6e65-4af3-ad50-58531688e895"


def test_unusable_event_dropped() -> None:
    assert normalize({}) is None
    assert normalize({"meta": {"domain": "en.wikipedia.org"}}) is None
    assert normalize({**EDIT, "revision": {}, "meta": {"domain": "en.wikipedia.org"}}) is None
