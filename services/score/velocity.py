"""Gap-score math (PLAN §6.6) — pure functions, no I/O.

`velocity_z` is the z-score of mention *acceleration* (EWMA-smoothed 2nd
derivative) over the rolling baseline. It is computed from the firehose
entity_tallies — never the embedded/throttled item count — so §6.3a admission
sampling cannot bias it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from statistics import mean, pstdev

EWMA_ALPHA = 0.3  # smoothing for the per-bucket mention series
MIN_BUCKETS = 12  # below this there is no baseline to z-score against
FLAT_BASELINE_Z = 10.0  # bounded stand-in for "burst out of total silence"
_EPS = 1e-9


def ewma(series: Sequence[float], alpha: float = EWMA_ALPHA) -> list[float]:
    out: list[float] = []
    prev = 0.0
    for i, x in enumerate(series):
        prev = x if i == 0 else alpha * x + (1 - alpha) * prev
        out.append(prev)
    return out


def velocity_z(series: Sequence[float], alpha: float = EWMA_ALPHA) -> float:
    """Z-score of the latest acceleration vs the rest of the window.

    `series` = mention counts per fixed-width bucket, oldest first, gap-filled
    with zeros (a silent bucket is a real observation of zero mentions).
    """
    if len(series) < MIN_BUCKETS:
        return 0.0
    s = ewma(series, alpha)
    accel = [s[i] - 2 * s[i - 1] + s[i - 2] for i in range(2, len(s))]
    baseline, latest = accel[:-1], accel[-1]
    sd = pstdev(baseline)
    if sd < _EPS:
        # Flat baseline: any nonzero acceleration is an infinite z; report a bounded
        # spike instead of dividing by ~0 (a brand-new entity bursting from silence).
        return 0.0 if abs(latest) < _EPS else FLAT_BASELINE_Z
    return (latest - mean(baseline)) / sd


def gap_score(
    velocity_z_: float,
    mainstream_presence: float,
    corroboration_weight: float,
    inauthenticity: float,
) -> float:
    """PLAN §6.6: gap = velocity_z × (1−mainstream) × corroboration × (1−inauth).

    Negative acceleration means a story is cooling off — clamp to 0, never a
    negative gap.
    """
    return (
        max(0.0, velocity_z_)
        * (1.0 - mainstream_presence)
        * corroboration_weight
        * (1.0 - inauthenticity)
    )


def fill_series(
    buckets: dict[datetime, float], since: datetime, until: datetime, step: timedelta
) -> list[float]:
    """Expand sparse (bucket_ts -> count) rows into a dense zero-filled series.

    `since` must already be aligned to the bucket grid (date_bin output aligns
    to the epoch, so align `since` the same way before calling).
    """
    out: list[float] = []
    t = since
    while t <= until:
        out.append(buckets.get(t, 0.0))
        t += step
    return out
