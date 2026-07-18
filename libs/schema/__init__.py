"""Shared Item/Story schema (PLAN §6.2/§6.4). Pure Pydantic — no DB/runtime deps.

DDL for the partitioned pgvector tables lives alongside in ``schema.sql``.
"""

from libs.schema.claim import ClaimRequest, ClaimResult
from libs.schema.enriched import EnrichedItem
from libs.schema.item import (
    EMBED_DIM,
    MAX_ENTITIES,
    MAX_TEXT_LEN,
    MAX_URLS,
    Entity,
    EntityType,
    Geo,
    Item,
    SourceClass,
    merge_entities,
)
from libs.schema.story import (
    CORROBORATION_WEIGHT,
    DEFAULT_N_CORROBORATION,
    Story,
    TrustState,
)
from libs.schema.tally import MAX_TALLY_ENTITIES, TallyFlush

__all__ = [
    "TallyFlush",
    "MAX_TALLY_ENTITIES",
    "Item",
    "Entity",
    "EntityType",
    "Geo",
    "SourceClass",
    "EnrichedItem",
    "ClaimRequest",
    "ClaimResult",
    "merge_entities",
    "EMBED_DIM",
    "MAX_TEXT_LEN",
    "MAX_ENTITIES",
    "MAX_URLS",
    "Story",
    "TrustState",
    "CORROBORATION_WEIGHT",
    "DEFAULT_N_CORROBORATION",
]
