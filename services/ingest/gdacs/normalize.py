"""GDACS event-list GeoJSON -> Item (pure, unit-testable; no network).

Feed: https://www.gdacs.org/gdacsapi/api/events/geteventlist/MAP — current disaster
events as GeoJSON Point Features. properties carries {eventtype (EQ/FL/TC/...),
eventid, episodeid, name, description, alertlevel (Green/Orange/Red), country,
fromdate, todate, datemodified, url{report,...}}. Timestamps are NAIVE but GDACS
publishes UTC, so UTC is attached explicitly. GDACS is AUTHORITATIVE (PLAN §6.1).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from libs.schema import MAX_TEXT_LEN, Entity, EntityType, Geo, Item, SourceClass

SOURCE = "gdacs"
MAX_COUNTRY_ENTITIES = 16


def _parse_dt(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def dedup_key(feature: dict[str, Any]) -> str:
    """Stable key that changes when GDACS revises an event episode."""
    props = feature.get("properties") or {}
    return (
        f"{props.get('eventtype', '')}:{props.get('eventid', '')}"
        f":{props.get('episodeid', '')}:{props.get('datemodified', '')}"
    )


def _point(geom: dict[str, Any] | None) -> Geo | None:
    if not isinstance(geom, dict) or geom.get("type") != "Point":
        return None
    coords = geom.get("coordinates") or []
    if not isinstance(coords, list) or len(coords) < 2:
        return None
    try:
        lon, lat = float(coords[0]), float(coords[1])
    except (TypeError, ValueError):
        return None
    if -180 <= lon <= 180 and -90 <= lat <= 90:
        return Geo(lat=lat, lon=lon)
    return None


def normalize(feature: dict[str, Any]) -> Item | None:
    """Return an Item, or None if the feature is unusable. Defensive: feed is
    external input, so never assume a field exists or has the right type."""
    props = feature.get("properties") or {}

    name = props.get("description") or props.get("name")
    if not name:
        return None
    alert = props.get("alertlevel")
    text = f"GDACS {alert} alert: {name}" if isinstance(alert, str) and alert else str(name)
    text = text[:MAX_TEXT_LEN]

    geo = _point(feature.get("geometry"))

    entities: list[Entity] = []
    country = props.get("country")
    if isinstance(country, str):
        for part in country.split(",")[:MAX_COUNTRY_ENTITIES]:
            part = part.strip()
            if part:
                entities.append(Entity(text=part[:256], type=EntityType.PLACE, geo=geo))

    urls = []
    url_map = props.get("url")
    report = url_map.get("report") if isinstance(url_map, dict) else None
    if isinstance(report, str) and report.startswith("http"):
        urls.append(report[:2_048])

    etype = props.get("eventtype")
    eid = props.get("eventid")
    if not etype or eid in (None, ""):
        return None
    key = f"gdacs:{etype}:{eid}"[:256]

    return Item(
        source=SOURCE,
        source_class=SourceClass.AUTHORITATIVE,
        ts_event=_parse_dt(props.get("fromdate")),
        text=text,
        geo=geo,
        entities=entities,
        urls=urls,
        content_hash=key,  # event id is the stable exact-dedup key across episodes
        raw_ref=key,
    )
