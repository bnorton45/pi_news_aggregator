"""Score worker tests (PLAN §6.6): velocity math, gap formula, alert gating."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from services.score.main import BIN, Scorer, _bin_floor
from services.score.velocity import (
    FLAT_BASELINE_Z,
    MIN_BUCKETS,
    fill_series,
    gap_score,
    velocity_z,
)

# ── velocity_z ────────────────────────────────────────────────────────────────


def test_short_series_has_no_baseline() -> None:
    assert velocity_z([5.0] * (MIN_BUCKETS - 1)) == 0.0


def test_steady_mentions_do_not_accelerate() -> None:
    assert velocity_z([10.0] * 100) == 0.0


def test_linear_growth_is_not_a_burst() -> None:
    # constant first derivative → ~zero second derivative everywhere
    assert abs(velocity_z([float(i) for i in range(100)])) < 3.0


def test_burst_out_of_silence_scores_flat_baseline_spike() -> None:
    series = [0.0] * 99 + [500.0]
    assert velocity_z(series) == FLAT_BASELINE_Z


def test_burst_over_noisy_baseline_scores_high() -> None:
    noise = [1.0, 2.0, 1.0, 3.0, 2.0, 1.0, 2.0, 3.0] * 12
    assert velocity_z(noise + [80.0]) > 3.0


def test_cooling_story_scores_negative() -> None:
    series = [1.0] * 80 + [50.0, 50.0, 50.0] + [0.0]
    assert velocity_z(series) < 0.0


# ── gap_score ─────────────────────────────────────────────────────────────────


def test_gap_formula() -> None:
    assert gap_score(4.0, 0.5, 0.7, 0.0) == 4.0 * 0.5 * 0.7


def test_gap_clamps_negative_velocity() -> None:
    assert gap_score(-3.0, 0.0, 1.0, 0.0) == 0.0


def test_rumor_weight_zeroes_gap() -> None:
    assert gap_score(50.0, 0.0, 0.0, 0.0) == 0.0


# ── fill_series ───────────────────────────────────────────────────────────────


def test_fill_series_zero_fills_gaps() -> None:
    t0 = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
    step = timedelta(minutes=5)
    sparse = {t0: 3.0, t0 + 2 * step: 7.0}
    assert fill_series(sparse, t0, t0 + 3 * step, step) == [3.0, 0.0, 7.0, 0.0]


# ── Scorer.cycle alert gating (fake DB) ───────────────────────────────────────


class _FakeDb:
    def __init__(self, stories: list[dict], earliest: datetime | None) -> None:
        self.stories = stories
        self.earliest = earliest
        self.buckets: dict[datetime, float] = {}
        self.updates: list[tuple] = []
        self.alerts: list[tuple] = []
        self.state: dict[str, Any] = {}

    async def earliest_bucket(self, baseline: timedelta) -> datetime | None:
        return self.earliest

    async def active_stories(self, window: timedelta, limit: int) -> list[dict]:
        return self.stories

    async def mention_series(
        self, entities: list[str], since: datetime, bin_width: timedelta
    ) -> dict[datetime, float]:
        return self.buckets

    async def mainstream_count(self, story_id) -> int:
        return 0

    async def update_scores(self, story_id, vz, presence, gap, inauthenticity) -> None:
        self.updates.append((story_id, vz, presence, gap))

    async def inauth_counts(
        self, story_id, sync_window, flag_threshold, rep_min_items, rep_bad_share
    ) -> tuple[int, int, int, int]:
        # Below the MIN_SOCIAL guard → inauthenticity 0; the §6.7 math itself is
        # covered by tests/test_inauth.py.
        return (0, 0, 0, 0)

    async def insert_alert(self, story_id, gap, vz, trust_state, cooldown) -> bool:
        self.alerts.append((story_id, gap, trust_state))
        return True

    async def upsert_system_state(self, key: str, value: dict) -> None:
        self.state[key] = value


def _story(trust: str) -> dict:
    return {
        "id": uuid4(),
        "first_seen": datetime.now(UTC),
        "entity_set": json.dumps(["Tokyo"]),
        "trust_state": trust,
        "independent_origins": 3,
    }


NOW = datetime(2026, 7, 5, 12, 34, 56, tzinfo=UTC)  # injected — no bin-boundary flake


def _burst_db(stories: list[dict]) -> _FakeDb:
    db = _FakeDb(stories, earliest=NOW - timedelta(days=3))  # warm baseline
    db.buckets = {_bin_floor(NOW): 500.0}  # burst out of silence in the latest bin
    return db


async def test_corroborated_burst_alerts() -> None:
    db = _burst_db([_story("corroborated")])
    await Scorer(db).cycle(NOW)  # type: ignore[arg-type]
    assert len(db.alerts) == 1
    (_, gap, _), (_, vz, _, gap2) = db.alerts[0], db.updates[0]
    assert vz == FLAT_BASELINE_Z
    assert gap == gap2 == FLAT_BASELINE_Z * 0.7  # presence 0, inauth 0, corroborated 0.7
    assert db.state["score"]["baseline_warming"] is False


async def test_rumor_never_alerts_even_on_burst() -> None:
    db = _burst_db([_story("rumor")])
    await Scorer(db).cycle(NOW)  # type: ignore[arg-type]
    assert db.alerts == []
    assert db.updates[0][3] == 0.0  # gap zeroed by corroboration weight


async def test_warming_baseline_holds_alerts() -> None:
    db = _FakeDb([_story("corroborated")], earliest=NOW - timedelta(hours=2))
    db.buckets = {_bin_floor(NOW): 500.0}
    await Scorer(db).cycle(NOW)  # type: ignore[arg-type]
    assert db.alerts == []  # scored, but alerting is held while warming
    assert db.updates and db.state["score"]["baseline_warming"] is True


def test_bin_floor_aligns_to_grid() -> None:
    ts = datetime(2026, 7, 5, 12, 7, 33, 123456, tzinfo=UTC)
    floored = _bin_floor(ts)
    assert floored.timestamp() % BIN.total_seconds() == 0
    assert floored <= ts < floored + BIN
