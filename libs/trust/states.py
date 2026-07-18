"""Trust-state transitions (PLAN §6.5) — pure, DB-free, unit-testable.

The states/weights themselves live in libs.schema.story (single source of truth,
shared with the Story model and the §6.6 gap score); this module owns only the
promotion rule.

| State           | Rule                                                    |
|-----------------|---------------------------------------------------------|
| RUMOR           | 1 independent origin                                    |
| CORROBORATED    | ≥ N independent origins (N=3) spanning ≥ 2 distinct sources |
| PRIMARY_BACKED  | a social claim matched to a primary/authoritative record|

The ≥2-distinct-sources floor on CORROBORATED (§6.5) stops single-platform
amplification — N distinct Bluesky accounts alone stay RUMOR — while preserving the
gap mission: cross-platform social (Bluesky+Mastodon) or social+local still promote
*before* mainstream. PRIMARY_BACKED is inherently multi-source (social ∧ primary),
so the floor never blocks it.

Promotion is monotonic: a Story never demotes (origin counts can only shrink
when members age past the 5-day wall, and a Story ages out with its Items —
demoting on the way to deletion would only flap alert eligibility). Only
CORROBORATED / PRIMARY_BACKED are alert-eligible; RUMOR is badged, never alerted.
"""

from __future__ import annotations

from libs.schema.story import (
    DEFAULT_N_CORROBORATION,
    MIN_CORROBORATION_SOURCES,
    TrustState,
)

_RANK = {TrustState.RUMOR: 0, TrustState.CORROBORATED: 1, TrustState.PRIMARY_BACKED: 2}


def next_state(
    current: TrustState,
    origins: int,
    *,
    distinct_sources: int = MIN_CORROBORATION_SOURCES,
    primary_matched: bool = False,
    n: int = DEFAULT_N_CORROBORATION,
    min_sources: int = MIN_CORROBORATION_SOURCES,
) -> TrustState:
    """The state implied by the evidence, floored at `current` (monotonic).

    `distinct_sources` = distinct feeds/platforms across the independent origins
    (`libs.trust.graph.distinct_origin_sources`); its default assumes diversity so
    count-threshold callers need not pass it, but the real cluster caller always does.
    """
    if primary_matched:
        implied = TrustState.PRIMARY_BACKED
    elif origins >= n and distinct_sources >= min_sources:
        implied = TrustState.CORROBORATED
    else:
        implied = TrustState.RUMOR
    return implied if _RANK[implied] > _RANK[current] else current
