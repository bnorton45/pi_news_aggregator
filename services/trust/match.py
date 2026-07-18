"""Primary-record matching (PLAN §6.5) — pure, DB-free, unit-testable.

`PRIMARY_BACKED` = a *social* claim matched to an authoritative/primary record by
entity ∧ geo ∧ time alignment ("big quake in X" + a USGS event, same region and
time). The record IS the evidence, so promotion needs no origin count.

Alignment rules:
- entity: ≥1 shared case-folded entity surface form (the cheap NER output, §6.3
  step 4 — corroboration runs on that, not on deep extraction).
- time: the SQL prefilter bounds candidates to ±window; nothing further here.
- geo: only constrains when BOTH sides carry coordinates — then they must be
  within GEO_KM_MAX. One-sided/absent geo does not veto an entity+time match
  (most social posts have no coordinates).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from uuid import UUID

GEO_KM_MAX = 500.0  # same-region scale: a quake felt + reported across a province


@dataclass(frozen=True)
class MatchCandidate:
    """A primary/authoritative item inside the time window."""

    item_id: UUID
    entity_texts: frozenset[str]  # case-folded
    lat: float | None = None
    lon: float | None = None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def find_primary_match(
    claim_entities: set[str],
    claim_lat: float | None,
    claim_lon: float | None,
    candidates: list[MatchCandidate],
) -> UUID | None:
    """First candidate aligned with the claim, or None. Candidates are expected
    newest-first or relevance-ordered by the caller; the rule is symmetric."""
    folded = {e.casefold() for e in claim_entities if e}
    if not folded:
        return None
    for c in candidates:
        if not (folded & c.entity_texts):
            continue
        both_geo = None not in (claim_lat, claim_lon, c.lat, c.lon)
        if both_geo and haversine_km(claim_lat, claim_lon, c.lat, c.lon) > GEO_KM_MAX:  # type: ignore[arg-type]
            continue
        return c.item_id
    return None
