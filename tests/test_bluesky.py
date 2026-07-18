"""Bluesky Jetstream normalize/should_keep tests — fixtures mirror live commit events."""

from services.ingest.bluesky.normalize import _uri_key, normalize, should_keep

POST = {
    "did": "did:plc:3nzvqu4r2lli5wcmamhmsfvt",
    "time_us": 1783122202602698,
    "kind": "commit",
    "commit": {
        "rev": "3mpptv2s7xc2h",
        "operation": "create",
        "collection": "app.bsky.feed.post",
        "rkey": "3mpptv2onqc2f",
        # pragma: allowlist nextline secret
        "cid": "bafyreib3fmvbfoxk36ptu5humm4pgd4vocpa4yjb7ipmyvxwsjjbmyvfoe",
        "record": {
            "$type": "app.bsky.feed.post",
            "createdAt": "2026-07-03T23:18:10.074Z",
            "langs": ["en"],
            "text": "Magnitude 6 quake felt in Guatemala City",
            "facets": [
                {
                    "features": [
                        {
                            "$type": "app.bsky.richtext.facet#link",
                            "uri": "https://earthquake.usgs.gov/earthquakes/eventpage/us6000abcd",
                        }
                    ],
                    "index": {"byteEnd": 24, "byteStart": 0},
                }
            ],
        },
    },
}


def test_keep_en_created_post() -> None:
    assert should_keep(POST) is True


def test_shaping_drops_noise() -> None:
    assert should_keep({**POST, "kind": "identity"}) is False
    assert should_keep({**POST, "commit": {**POST["commit"], "operation": "delete"}}) is False
    assert (
        should_keep({**POST, "commit": {**POST["commit"], "collection": "app.bsky.feed.like"}})
        is False
    )
    rec = {**POST["commit"]["record"], "langs": ["ja"]}
    assert should_keep({**POST, "commit": {**POST["commit"], "record": rec}}) is False
    rec = {**POST["commit"]["record"], "langs": ["en-US"]}
    assert should_keep({**POST, "commit": {**POST["commit"], "record": rec}}) is True
    rec = {**POST["commit"]["record"]}
    del rec["langs"]
    assert should_keep({**POST, "commit": {**POST["commit"], "record": rec}}) is False
    rec = {**POST["commit"]["record"], "text": "   "}
    assert should_keep({**POST, "commit": {**POST["commit"], "record": rec}}) is False


def test_normalize_post() -> None:
    item = normalize(POST)
    assert item is not None
    assert item.source == "bluesky"
    assert item.source_class.value == "social"
    assert item.text == "Magnitude 6 quake felt in Guatemala City"
    assert item.lang == "en"
    assert item.urls == ["https://earthquake.usgs.gov/earthquakes/eventpage/us6000abcd"]
    assert item.ts_event is not None and item.ts_event.tzinfo is not None
    assert item.raw_ref.startswith("bafyrei")


def test_identity_is_hashed_not_raw() -> None:
    item = normalize(POST)
    assert item is not None
    assert "did:plc" not in item.author_ref
    assert "did:plc" not in item.content_hash
    assert item.content_hash.startswith("bsky:")


def test_reply_links_parent_via_same_hash() -> None:
    parent_uri = "at://did:plc:qp7anqcqescucfwi4wari2z4/app.bsky.feed.post/3mpptuq5p422m"
    rec = {**POST["commit"]["record"], "reply": {"parent": {"uri": parent_uri, "cid": "bafy..."}}}
    item = normalize({**POST, "commit": {**POST["commit"], "record": rec}})
    assert item is not None
    assert item.parent_ref == _uri_key(parent_uri)
    assert "did:plc" not in item.parent_ref


def test_unusable_event_dropped() -> None:
    assert normalize({}) is None
    assert normalize({"did": "did:plc:x", "commit": {"rkey": "r", "record": {}}}) is None
