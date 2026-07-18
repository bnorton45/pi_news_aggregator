"""Story schema (PLAN §6.4/§6.5/§6.6).

A Story is an online cluster of Items plus its provenance graph and trust state.
Stories live entirely inside the 5-day window and age out with their Items.
"""

from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class TrustState(str, enum.Enum):
    """Corroboration state (PLAN §6.5). Only the latter two are alert-eligible."""

    RUMOR = "rumor"  # 1 independent origin
    CORROBORATED = "corroborated"  # >= N independent origins (default N=3)
    PRIMARY_BACKED = "primary_backed"  # social claim matched to an authoritative/primary record


# Corroboration weights feeding the gap score (PLAN §6.6).
CORROBORATION_WEIGHT: dict[TrustState, float] = {
    TrustState.RUMOR: 0.0,
    TrustState.CORROBORATED: 0.7,
    TrustState.PRIMARY_BACKED: 1.0,
}

DEFAULT_N_CORROBORATION = 3  # PLAN §6.5 default
# CORROBORATED also requires the independent origins to span this many distinct
# sources (feeds/platforms), so N accounts on one platform (e.g. Bluesky-only)
# cannot forge corroboration — the gap mission still fires cross-platform social
# (Bluesky+Mastodon) before mainstream picks it up (PLAN §6.5).
MIN_CORROBORATION_SOURCES = 2


class Story(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    first_seen: datetime  # partition key = date(first_seen)
    last_seen: datetime

    entity_set: list[str] = Field(default_factory=list, max_length=512)
    member_item_ids: list[UUID] = Field(default_factory=list)
    source_set: list[str] = Field(default_factory=list, max_length=128)

    independent_origins: int = 0  # weakly-connected components, org/domain-deduped (§6.5)
    trust_state: TrustState = TrustState.RUMOR

    # Gap-score components (§6.6); populated by the score layer.
    velocity_z: float = 0.0
    mainstream_presence: float = 0.0  # [0,1]
    inauthenticity: float = 0.0  # [0,1]
    gap: float = 0.0

    def corroboration_weight(self) -> float:
        return CORROBORATION_WEIGHT[self.trust_state]
