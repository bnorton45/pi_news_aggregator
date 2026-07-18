"""GDACS normalize tests — fixtures mirror real gdacsapi geteventlist/MAP shapes."""

from datetime import UTC

from services.ingest.gdacs.normalize import dedup_key, normalize

FULL = {
    "geometry": {"type": "Point", "coordinates": [-90.5, 14.6]},
    "properties": {
        "eventtype": "EQ",
        "eventid": 1103888,
        "episodeid": 24,
        "name": "Earthquake in Guatemala",
        "description": "Earthquake in Guatemala",
        "alertlevel": "Orange",
        "country": "Guatemala, Mexico",
        "fromdate": "2026-07-01T01:00:00",
        "todate": "2026-07-03T01:00:00",
        "datemodified": "2026-07-03T11:16:47",
        "url": {"report": "https://www.gdacs.org/report.aspx?eventid=1103888"},
    },
}


def test_normalize_full_event() -> None:
    item = normalize(FULL)
    assert item is not None
    assert item.source == "gdacs"
    assert item.source_class.value == "authoritative"
    assert item.text == "GDACS Orange alert: Earthquake in Guatemala"
    assert item.content_hash == "gdacs:EQ:1103888"
    assert item.urls == ["https://www.gdacs.org/report.aspx?eventid=1103888"]
    assert [e.text for e in item.entities] == ["Guatemala", "Mexico"]


def test_naive_timestamp_gets_utc() -> None:
    item = normalize(FULL)
    assert item is not None
    assert item.ts_event is not None
    assert item.ts_event.tzinfo is UTC


def test_point_geo() -> None:
    item = normalize(FULL)
    assert item is not None and item.geo is not None
    assert item.geo.lat == 14.6 and item.geo.lon == -90.5


def test_dedup_key_tracks_episode_updates() -> None:
    k1 = dedup_key(FULL)
    bumped = {
        **FULL,
        "properties": {
            **FULL["properties"],
            "episodeid": 25,
            "datemodified": "2026-07-03T12:00:00",
        },
    }
    assert dedup_key(bumped) != k1
    assert dedup_key(FULL) == k1


def test_missing_ids_dropped() -> None:
    props = {**FULL["properties"]}
    del props["eventtype"]
    assert normalize({**FULL, "properties": props}) is None
    props = {**FULL["properties"], "eventid": None}
    assert normalize({**FULL, "properties": props}) is None


def test_unusable_feature_dropped() -> None:
    assert normalize({}) is None
    assert normalize({"properties": {"eventtype": "FL", "eventid": 1}}) is None


def test_bad_geometry_survivable() -> None:
    item = normalize({**FULL, "geometry": {"type": "Point", "coordinates": ["x"]}})
    assert item is not None
    assert item.geo is None
