"""NOAA/NWS active-alerts GeoJSON -> Item (pure, unit-testable; no network).

Feed: https://api.weather.gov/alerts/active — CAP alerts as GeoJSON Features.
properties carries {id (urn), event, headline, severity, areaDesc, sent, effective,
onset, status, messageType}; geometry is a Polygon for storm-warned polygons and
null for zone-based alerts. NOAA/NWS is AUTHORITATIVE (PLAN §6.1), so its Items
can promote a Story to PRIMARY_BACKED.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from libs.schema import MAX_TEXT_LEN, Entity, EntityType, Geo, Item, SourceClass

SOURCE = "noaa"
MAX_AREA_ENTITIES = 32  # areaDesc can list dozens of counties; keep the cheap signal bounded


def _parse_dt(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def dedup_key(feature: dict[str, Any]) -> str:
    """Stable key that changes when NWS re-issues an alert (`sent` bumps on updates)."""
    props = feature.get("properties") or {}
    return f"{props.get('id', '')}:{props.get('sent', '')}"


def _centroid(geom: dict[str, Any] | None) -> Geo | None:
    """Cheap centroid of a warned Polygon's exterior ring; None for zone-based alerts."""
    if not isinstance(geom, dict) or geom.get("type") != "Polygon":
        return None
    rings = geom.get("coordinates") or []
    ring = rings[0] if isinstance(rings, list) and rings else []
    pts = []
    for pt in ring if isinstance(ring, list) else []:
        try:
            lon, lat = float(pt[0]), float(pt[1])
        except (TypeError, ValueError, IndexError):
            continue
        if -180 <= lon <= 180 and -90 <= lat <= 90:
            pts.append((lon, lat))
    if not pts:
        return None
    lon = sum(p[0] for p in pts) / len(pts)
    lat = sum(p[1] for p in pts) / len(pts)
    return Geo(lat=lat, lon=lon)


def normalize(feature: dict[str, Any]) -> Item | None:
    """Return an Item, or None if the feature is unusable or not an actual alert.
    Defensive: feed is external input, so never assume a field exists or has the
    right type."""
    props = feature.get("properties") or {}

    # Test/Exercise/Draft/System alerts are operational noise, not events.
    if props.get("status") != "Actual":
        return None

    headline = props.get("headline")
    event = props.get("event")
    area = props.get("areaDesc")
    if headline:
        text = str(headline)
    elif event and area:
        text = f"{event} - {area}"
    elif event:
        text = str(event)
    else:
        return None
    text = text[:MAX_TEXT_LEN]

    geo = _centroid(feature.get("geometry"))

    entities: list[Entity] = []
    if isinstance(area, str):
        for part in area.split(";")[:MAX_AREA_ENTITIES]:
            part = part.strip()
            if part:
                entities.append(Entity(text=part[:256], type=EntityType.PLACE))

    urls = []
    fid = feature.get("id")
    if isinstance(fid, str) and fid.startswith("http"):
        urls.append(fid[:2_048])

    urn = str(props.get("id") or feature.get("id") or "")[:256]
    if not urn:
        return None

    ts_event = (
        _parse_dt(props.get("onset"))
        or _parse_dt(props.get("effective"))
        or _parse_dt(props.get("sent"))
    )

    return Item(
        source=SOURCE,
        source_class=SourceClass.AUTHORITATIVE,
        ts_event=ts_event,
        text=text,
        geo=geo,
        entities=entities,
        urls=urls,
        content_hash=urn,  # CAP urn is a stable exact-dedup key
        raw_ref=urn,
    )
