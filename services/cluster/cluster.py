"""Online-clustering decision logic (PLAN §6.4) — pure, DB-free, unit-testable.

The DB layer (db.py) does the partition-pruned ANN query; this module decides what to
do with the neighbours it returns: assign the new item to the nearest Story that is both
**similar enough** (cosine ≥ θ) and **shares an entity** (defeats coincidental vector
proximity), else open a new Story. Time-window pruning is applied in SQL.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from libs.dedup.simhash import from_signed64
from libs.schema import Entity
from libs.trust import ProvNode, canonical_url


@dataclass(frozen=True)
class Neighbor:
    """A scored candidate from the ANN query (already time-window pruned, nearest-first)."""

    story_id: UUID
    similarity: float  # cosine in [-1, 1]
    entity_texts: frozenset[str]


def entity_texts(entities: list[Entity]) -> set[str]:
    """Entity surface forms used for the shared-entity test (case-folded)."""
    return {e.text.casefold() for e in entities}


def choose_story(new_entities: set[str], neighbors: list[Neighbor], theta: float) -> UUID | None:
    """Return the Story to join, or None to open a new one.

    Joins the nearest neighbour that clears the cosine threshold **and** shares at least
    one entity. An item with no entities never merges (it opens its own Story) — strict
    per §6.4; loosen only with evidence.
    """
    folded = {e.casefold() for e in new_entities}
    if not folded:
        return None
    for n in neighbors:  # nearest-first
        if n.similarity >= theta and folded & n.entity_texts:
            return n.story_id
    return None


def is_candidate(independent_origins: int, threshold: int = 2) -> bool:
    """Whether a Story is worth spending the claim LLM on (PLAN §6.3 llm.heavy gate): enough
    independent origins that it is developing, not a single-source rumour."""
    return independent_origins >= threshold


def prov_node(row: Any) -> ProvNode:
    """ProvNode from a stored member row (§6.5): urls canonicalized on read, simhash
    mapped back from Postgres' signed bigint."""
    urls = frozenset(filter(None, (canonical_url(u) for u in json.loads(row["urls"]))))
    return ProvNode(
        item_id=row["id"],
        author_ref=row["author_ref"] or "",
        parent_ref=row["parent_ref"],
        content_hash=row["content_hash"] or "",
        urls=urls,
        simhash=from_signed64(row["simhash"] or 0),
        source=row["source"] or "",
        source_class=row["source_class"] or "",
        wire_ref=row["wire_ref"] or "",
    )


def in_story_primary_match(source_classes: set[str]) -> bool:
    """§6.5 primary-match, cheap in-story form: a social claim and an authoritative/
    primary record clustered into the same Story already align on entity ∧ semantics ∧
    time (§6.4's merge criteria), which is the promotion evidence. The claim-level
    entity∧geo∧time matcher (claimx path) is the second, finer-grained promoter."""
    return "social" in source_classes and bool(source_classes & {"authoritative", "primary"})
