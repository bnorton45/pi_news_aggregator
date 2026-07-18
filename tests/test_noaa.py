"""NOAA/NWS normalize tests — fixtures mirror real api.weather.gov/alerts/active shapes."""

from services.ingest.noaa.normalize import dedup_key, normalize

FULL = {
    "id": "https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.abc.001.1",
    "geometry": {
        "type": "Polygon",
        "coordinates": [[[-80.0, 41.0], [-80.2, 41.0], [-80.1, 41.4], [-80.0, 41.0]]],
    },
    "properties": {
        "id": "urn:oid:2.49.0.1.840.0.abc.001.1",
        "event": "Severe Thunderstorm Warning",
        "headline": "Severe Thunderstorm Warning issued July 3 at 7:20PM EDT by NWS Pittsburgh PA",
        "severity": "Severe",
        "areaDesc": "Mercer, PA; Crawford, PA",
        "sent": "2026-07-03T19:20:00-04:00",
        "effective": "2026-07-03T19:20:00-04:00",
        "onset": "2026-07-03T19:20:00-04:00",
        "status": "Actual",
        "messageType": "Update",
    },
}


def test_normalize_full_alert() -> None:
    item = normalize(FULL)
    assert item is not None
    assert item.source == "noaa"
    assert item.source_class.value == "authoritative"
    assert item.text.startswith("Severe Thunderstorm Warning issued")
    assert item.content_hash == "urn:oid:2.49.0.1.840.0.abc.001.1"
    assert item.urls == ["https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.abc.001.1"]
    assert item.ts_event is not None and item.ts_event.tzinfo is not None
    assert [e.text for e in item.entities] == ["Mercer, PA", "Crawford, PA"]
    assert all(e.type.value == "place" for e in item.entities)


def test_polygon_centroid() -> None:
    item = normalize(FULL)
    assert item is not None and item.geo is not None
    assert 40.9 < item.geo.lat < 41.5
    assert -80.3 < item.geo.lon < -79.9


def test_zone_alert_has_no_geo() -> None:
    feat = {**FULL, "geometry": None}
    item = normalize(feat)
    assert item is not None
    assert item.geo is None


def test_non_actual_status_dropped() -> None:
    for status in ("Test", "Exercise", "Draft", None):
        feat = {**FULL, "properties": {**FULL["properties"], "status": status}}
        assert normalize(feat) is None


def test_headline_fallback_to_event_area() -> None:
    props = {**FULL["properties"]}
    del props["headline"]
    item = normalize({**FULL, "properties": props})
    assert item is not None
    assert item.text == "Severe Thunderstorm Warning - Mercer, PA; Crawford, PA"


def test_unusable_feature_dropped() -> None:
    assert normalize({}) is None
    assert normalize({"properties": {"status": "Actual"}}) is None


def test_bad_geometry_is_survivable() -> None:
    feat = {
        **FULL,
        "geometry": {"type": "Polygon", "coordinates": [[["x", "y"], [None, 1]]]},
    }
    item = normalize(feat)
    assert item is not None
    assert item.geo is None


def test_dedup_key_tracks_reissue() -> None:
    k1 = dedup_key(FULL)
    resent = {**FULL, "properties": {**FULL["properties"], "sent": "2026-07-03T19:40:00-04:00"}}
    assert dedup_key(resent) != k1
    assert dedup_key(FULL) == k1


def test_naive_timestamp_rejected() -> None:
    feat = {
        **FULL,
        "properties": {
            **FULL["properties"],
            "onset": "2026-07-03T19:20:00",
            "effective": "not a date",
            "sent": 12345,
        },
    }
    item = normalize(feat)
    assert item is not None
    assert item.ts_event is None
