"""USGS earthquake GeoJSON -> Item (pure, unit-testable; no network).

Feed: https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php
Each Feature carries properties{mag,place,time,updated,url,title} and
geometry{coordinates:[lon,lat,depth]}. USGS is an AUTHORITATIVE source (PLAN §6.1),
so its Items can promote a Story to PRIMARY_BACKED.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from libs.schema import Entity, EntityType, Geo, Item, SourceClass

SOURCE = "usgs"


def _ms_to_dt(ms: Any) -> datetime | None:
    if not isinstance(ms, int | float):
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def dedup_key(feature: dict[str, Any]) -> str:
    """Stable key that changes when USGS revises an event (mag/place updates)."""
    fid = str(feature.get("id", ""))
    updated = (feature.get("properties") or {}).get("updated", "")
    return f"{fid}:{updated}"


def normalize(feature: dict[str, Any]) -> Item | None:
    """Return an Item, or None if the feature is unusable. Defensive: feed is
    external input, so never assume a field exists or has the right type."""
    props = feature.get("properties") or {}
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates") or []

    title = props.get("title")
    place = props.get("place")
    mag = props.get("mag")
    if title:
        text = str(title)
    elif place and mag is not None:
        text = f"M{mag} - {place}"
    elif place:
        text = str(place)
    else:
        return None
    text = text[:8_192]

    geo = None
    if isinstance(coords, list) and len(coords) >= 2:
        try:
            lon, lat = float(coords[0]), float(coords[1])
            if -180 <= lon <= 180 and -90 <= lat <= 90:
                geo = Geo(lat=lat, lon=lon)
        except (TypeError, ValueError):
            geo = None

    entities: list[Entity] = []
    if place:
        entities.append(Entity(text=str(place)[:256], type=EntityType.PLACE, geo=geo))

    urls = []
    url = props.get("url")
    if isinstance(url, str) and url:
        urls.append(url[:2_048])

    fid = str(feature.get("id", ""))[:256]

    return Item(
        source=SOURCE,
        source_class=SourceClass.AUTHORITATIVE,
        ts_event=_ms_to_dt(props.get("time")),
        text=text,
        geo=geo,
        entities=entities,
        urls=urls,
        content_hash=fid,  # USGS event id is a stable exact-dedup key
        raw_ref=fid,
    )
