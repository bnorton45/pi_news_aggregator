"""Coordinated-inauthenticity math (PLAN §6.7) — pure functions, no I/O.

Per-Story `inauthenticity` in [0,1] from four in-window signals (the 5-day wall
forbids any long-term reputation store; everything here is recomputed each score
cycle from window data):

  * amplification — many social items from few independent origins (§6.5's WCC
    origin count already collapses same-account/same-outlet items).
  * copy density  — COPY/AUTHOR provenance-edge share: copypasta networks and
    single-account floods leave exactly these edges (libs/trust/edges.py).
  * synchronization — share of social items observed within SYNC_WINDOW of
    another social item in the same Story: bot fleets post together; organic
    stories arrive spread out.
  * low-reputation share — share of the Story's social items authored by
    in-window repeat offenders: authors with ≥ REP_MIN_ITEMS social items of
    which ≥ REP_BAD_SHARE landed in Stories already flagged inauthentic
    (feedback across cycles is deliberate — that IS the 5d source reputation;
    it decays with the window).

PLAN §6.7 also names "low-age accounts"; account age is not ingested on Item
(author_ref is a hashed id, §6.2) so that signal is unavailable by design.

Stories with fewer than MIN_SOCIAL social items score 0.0: sparse data must
never be penalized (a two-item story trivially has low diversity).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Weights sum to 1.0 so the combined score stays in [0,1] without renormalizing.
W_AMPLIFICATION = float(os.environ.get("INAUTH_W_AMPLIFICATION", "0.30"))
W_COPY = float(os.environ.get("INAUTH_W_COPY", "0.30"))
W_SYNC = float(os.environ.get("INAUTH_W_SYNC", "0.20"))
W_LOW_REP = float(os.environ.get("INAUTH_W_LOW_REP", "0.20"))

MIN_SOCIAL = int(os.environ.get("INAUTH_MIN_SOCIAL", "5"))
# An author is a "repeat offender" only with enough window history to judge:
REP_MIN_ITEMS = int(os.environ.get("INAUTH_REP_MIN_ITEMS", "3"))
REP_BAD_SHARE = float(os.environ.get("INAUTH_REP_BAD_SHARE", "0.5"))
# Stories at/above this inauthenticity feed the author-reputation signal and the
# weak_labels CIB-negative branch (keep in sync with the literal in schema.sql).
FLAG_THRESHOLD = float(os.environ.get("INAUTH_FLAG_THRESHOLD", "0.7"))


@dataclass(frozen=True)
class StorySignals:
    """Per-Story aggregates the score DB layer extracts (services/score/db.py)."""

    social_items: int
    independent_origins: int  # §6.5 WCC count, already on stories
    copy_edges: int  # COPY + AUTHOR provenance edges within the story
    synced_items: int  # social items within SYNC_WINDOW of another one
    low_rep_items: int  # social items authored by in-window repeat offenders


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def inauthenticity(sig: StorySignals) -> float:
    """Weighted combination of the four §6.7 component signals, each in [0,1]."""
    n = sig.social_items
    if n < MIN_SOCIAL:
        return 0.0
    amplification = _clamp01(1.0 - sig.independent_origins / n)
    copy_density = _clamp01(sig.copy_edges / n)
    synchronization = _clamp01(sig.synced_items / n)
    low_rep = _clamp01(sig.low_rep_items / n)
    return _clamp01(
        W_AMPLIFICATION * amplification
        + W_COPY * copy_density
        + W_SYNC * synchronization
        + W_LOW_REP * low_rep
    )
