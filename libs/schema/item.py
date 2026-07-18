"""Common Item schema (PLAN §6.2).

The Item is the normalized unit every ingester emits and the only thing that
crosses the NATS boundary. It is validated + size-capped *before* anything
downstream touches it (PLAN §3.2). Treat all string fields as attacker-controlled.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

# Size caps — enforced at the boundary so a hostile source cannot exhaust memory
# downstream (PLAN §3.2: "schema-validated + size-capped at the NATS boundary").
MAX_TEXT_LEN = 8_192
MAX_URL_LEN = 2_048
MAX_URLS = 32
MAX_ENTITIES = 128
MAX_REF_LEN = 256

EMBED_DIM = 384  # bge-small class; MUST match vector(384) in schema.sql and libs/embed


class SourceClass(str, enum.Enum):
    """Trust tier of the originating source (drives corroboration, PLAN §6.5)."""

    AUTHORITATIVE = "authoritative"  # USGS, NOAA/NWS, GDACS, ReliefWeb
    PRIMARY = "primary"  # primary records / documents
    SOCIAL = "social"  # Bluesky, Mastodon — high noise, require corroboration
    MAINSTREAM = "mainstream"  # presence baseline ONLY, never content-scraped
    # Station RSS titles/summaries (never full-article scrape): near-primary for
    # local events, but wire-syndication-collapsed for origin counting (§6.5,
    # libs/trust/wire.py) and excluded from the mainstream presence baseline —
    # local coverage is the *early* side of the gap signal. The ingester lands
    # post-0b (docs/local-news-design.md); the class is wired through now so the
    # trust path is testable ahead of it.
    LOCAL_NEWS = "local_news"


class EntityType(str, enum.Enum):
    PERSON = "person"
    ORG = "org"
    PLACE = "place"
    OTHER = "other"


class Geo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    geohash: str | None = Field(default=None, max_length=16)


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=256)
    type: EntityType = EntityType.OTHER
    geo: Geo | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Item(BaseModel):
    """Normalized ingest unit. `extra="forbid"` => reject-and-drop on unknown fields."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    source: str = Field(max_length=64)  # "usgs", "bluesky", ...
    source_class: SourceClass

    ts_observed: datetime = Field(default_factory=_utcnow)  # partition key = date(ts_observed)
    ts_event: datetime | None = None

    lang: str = Field(default="und", max_length=8)
    text: str = Field(max_length=MAX_TEXT_LEN)

    entities: list[Entity] = Field(default_factory=list, max_length=MAX_ENTITIES)
    geo: Geo | None = None
    urls: list[str] = Field(default_factory=list, max_length=MAX_URLS)

    author_ref: str = Field(default="", max_length=MAX_REF_LEN)  # HASHED account id, never raw
    parent_ref: str | None = Field(default=None, max_length=MAX_REF_LEN)  # quote/reply target
    content_hash: str = Field(default="", max_length=MAX_REF_LEN)  # exact + simhash for dedup
    raw_ref: str = Field(default="", max_length=MAX_REF_LEN)  # pointer; raw blob also 5d-TTL


def merge_entities(item: Item, entities: list[Entity]) -> None:
    """Append entities not already present (by text+type), respecting MAX_ENTITIES, and
    fill coarse geo from the first geo-bearing entity if the item has none. Shared by the
    gazetteer tally (§6.3 step 2) and survivor NER (§6.3 step 4)."""
    have = {(e.text, e.type) for e in item.entities}
    for ent in entities:
        if (ent.text, ent.type) in have or len(item.entities) >= MAX_ENTITIES:
            continue
        item.entities.append(ent)
        have.add((ent.text, ent.type))
        if item.geo is None and ent.geo is not None:
            item.geo = ent.geo
