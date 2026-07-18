"""§6.7 coordinated-inauthenticity math tests (services/score/inauth.py)."""

from __future__ import annotations

from services.score.inauth import (
    MIN_SOCIAL,
    W_AMPLIFICATION,
    W_COPY,
    StorySignals,
    inauthenticity,
)


def _sig(
    social: int = 20,
    origins: int = 20,
    copy_edges: int = 0,
    synced: int = 0,
    low_rep: int = 0,
) -> StorySignals:
    return StorySignals(
        social_items=social,
        independent_origins=origins,
        copy_edges=copy_edges,
        synced_items=synced,
        low_rep_items=low_rep,
    )


def test_organic_story_scores_zero() -> None:
    # Every item an independent origin, no copy edges, spread arrivals, clean authors.
    assert inauthenticity(_sig()) == 0.0


def test_sparse_story_is_never_penalized() -> None:
    # Worst possible signals, but below the MIN_SOCIAL guard → 0.
    worst = _sig(social=MIN_SOCIAL - 1, origins=1, copy_edges=10, synced=4, low_rep=4)
    assert inauthenticity(worst) == 0.0


def test_single_origin_amplification_scores_the_amp_weight() -> None:
    # 20 social items collapsing to 1 origin, nothing else suspicious.
    score = inauthenticity(_sig(origins=1))
    assert abs(score - W_AMPLIFICATION * (1 - 1 / 20)) < 1e-9


def test_copypasta_network_scores_high() -> None:
    # Copy edges everywhere + few origins + synchronized burst = the CIB shape.
    cib = _sig(social=40, origins=2, copy_edges=40, synced=36, low_rep=0)
    organic = _sig(social=40, origins=30, copy_edges=2, synced=4, low_rep=0)
    assert inauthenticity(cib) > 0.6
    assert inauthenticity(cib) > inauthenticity(organic)


def test_repeat_offender_authors_raise_the_score() -> None:
    base = _sig(social=20, origins=10)
    tainted = _sig(social=20, origins=10, low_rep=20)
    assert inauthenticity(tainted) > inauthenticity(base)


def test_score_is_clamped_to_unit_interval() -> None:
    # Component ratios can exceed 1 (copy edges are pairwise, not per-item).
    flood = _sig(social=10, origins=1, copy_edges=500, synced=10, low_rep=10)
    assert 0.0 <= inauthenticity(flood) <= 1.0


def test_copy_density_alone_uses_the_copy_weight() -> None:
    score = inauthenticity(_sig(social=10, copy_edges=10, origins=10))
    assert abs(score - W_COPY) < 1e-9
