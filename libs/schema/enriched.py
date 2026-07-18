"""EnrichedItem — the inference -> db-writer message (PLAN §3.3 split, §6.3).

The inference worker publishes one of these on ``enriched.<source>`` after embedding;
the db-writer consumes ``enriched.>`` and writes Item + vector to pgvector. The
embedding length is pinned to EMBED_DIM so a malformed payload is rejected at the
NATS boundary like any other untrusted input (PLAN §3.2).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from libs.schema.item import EMBED_DIM, Item


class EnrichedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: Item
    embedding: list[float] = Field(min_length=EMBED_DIM, max_length=EMBED_DIM)
    # §6.3a exploration quota: True = embedded from BELOW the admission threshold
    # (shed-tail counterfactual for the filter retrain loop, not a real admission).
    exploration: bool = False
