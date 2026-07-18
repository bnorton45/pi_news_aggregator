"""Score worker (PLAN §6.6/§6.8): velocity_z + mainstream_presence + gap + alerts.

DB-only pod — no NATS at all. Each cycle it re-scores the recently-active Stories
from the firehose entity_tallies (written by the db-writer off the enrich flushes,
so §6.3a admission sampling cannot bias the signal), updates the Story score
columns, raises alert rows for `gap > threshold ∧ trust_state ≥ CORROBORATED`,
and publishes its §6.8 health beat (baseline warming) into system_state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from libs.schema import CORROBORATION_WEIGHT, TrustState
from services.score.inauth import (
    FLAG_THRESHOLD,
    REP_BAD_SHARE,
    REP_MIN_ITEMS,
    StorySignals,
    inauthenticity,
)
from services.score.velocity import fill_series, gap_score, velocity_z

if TYPE_CHECKING:  # deferred: keeps the pure scoring logic importable without asyncpg
    from services.score.db import ScoreDb

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("score")

INTERVAL_S = float(os.environ.get("SCORE_INTERVAL_S", "60"))
ACTIVE_WINDOW = timedelta(hours=float(os.environ.get("SCORE_ACTIVE_H", "48")))
BASELINE = timedelta(days=float(os.environ.get("SCORE_BASELINE_D", "5")))
BIN = timedelta(minutes=float(os.environ.get("SCORE_BIN_MIN", "5")))
MAX_STORIES = int(os.environ.get("SCORE_MAX_STORIES", "200"))
GAP_THRESHOLD = float(os.environ.get("GAP_ALERT_THRESHOLD", "2.0"))
ALERT_COOLDOWN = timedelta(hours=float(os.environ.get("ALERT_COOLDOWN_H", "6")))
# Below this much tally history the 5-day baseline is meaningless — flag
# "baseline warming" (§6.8) and hold alerts rather than alert off noise.
WARM_MIN = timedelta(hours=float(os.environ.get("SCORE_WARM_MIN_H", "12")))
# mainstream_presence saturates at this many mainstream-class items in a Story.
MAINSTREAM_SATURATION = int(os.environ.get("MAINSTREAM_SATURATION", "3"))
# §6.7 synchronization window: a social item observed within this of another
# social item in the same Story counts as "synchronized" (bot fleets post together).
SYNC_WINDOW = timedelta(seconds=float(os.environ.get("INAUTH_SYNC_WINDOW_S", "60")))


def _bin_floor(ts: datetime) -> datetime:
    """Align to the same epoch-anchored grid as SQL date_bin."""
    seconds = int(BIN.total_seconds())
    return datetime.fromtimestamp(int(ts.timestamp()) // seconds * seconds, tz=UTC)


class Scorer:
    def __init__(self, db: ScoreDb) -> None:
        self.db = db
        self.scored = 0
        self.alerts = 0

    async def cycle(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        earliest = await self.db.earliest_bucket(BASELINE)
        warming = earliest is None or (now - earliest) < WARM_MIN
        # Baseline starts where tally history starts (clamped to the window): an
        # entity that never appeared before still gets a full zero baseline, so a
        # brand-new burst z-scores high instead of hiding behind a short series.
        since = _bin_floor(max(now - BASELINE, earliest)) if earliest else _bin_floor(now)

        stories = await self.db.active_stories(ACTIVE_WINDOW, MAX_STORIES)
        alerted = 0
        flagged = 0
        for s in stories:
            entities = json.loads(s["entity_set"])
            if not entities:
                continue
            buckets = await self.db.mention_series(entities, since, BIN)
            vz = velocity_z(fill_series(buckets, since, _bin_floor(now), BIN))
            n_main = await self.db.mainstream_count(s["id"])
            presence = min(1.0, n_main / MAINSTREAM_SATURATION)
            trust = TrustState(s["trust_state"])
            social, synced, copy_edges, low_rep = await self.db.inauth_counts(
                s["id"], SYNC_WINDOW, FLAG_THRESHOLD, REP_MIN_ITEMS, REP_BAD_SHARE
            )
            inauth = inauthenticity(
                StorySignals(
                    social_items=social,
                    independent_origins=s["independent_origins"],
                    copy_edges=copy_edges,
                    synced_items=synced,
                    low_rep_items=low_rep,
                )
            )
            flagged += inauth >= FLAG_THRESHOLD
            gap = gap_score(vz, presence, CORROBORATION_WEIGHT[trust], inauth)
            await self.db.update_scores(s["id"], vz, presence, gap, inauth)
            self.scored += 1
            # RUMOR carries weight 0 so it can never cross the threshold — the
            # trust check is still stated explicitly (PLAN §6.6 alert rule).
            if not warming and gap > GAP_THRESHOLD and trust is not TrustState.RUMOR:
                if await self.db.insert_alert(s["id"], gap, vz, trust.value, ALERT_COOLDOWN):
                    alerted += 1
                    log.info("ALERT story=%s gap=%.2f vz=%.2f trust=%s", s["id"], gap, vz, trust)
        self.alerts += alerted
        await self.db.upsert_system_state(
            "score",
            {
                "baseline_warming": warming,
                "stories_scored": len(stories),
                "stories_flagged_inauthentic": flagged,
                "alerts_raised": alerted,
                "last_run": now.isoformat(),
            },
        )
        log.info(
            "cycle: stories=%d alerts=%d warming=%s (total scored=%d alerts=%d)",
            len(stories),
            alerted,
            warming,
            self.scored,
            self.alerts,
        )


async def run() -> None:
    from services.score.db import ScoreDb

    db = ScoreDb()
    scorer = Scorer(db)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    # Retry, don't crash-exit: at install time Postgres readiness races this pod,
    # and a CrashLoopBackOff here can stall the whole helm --wait (k3d e2e).
    while not stop.is_set():
        try:
            await db.connect()
            break
        except Exception as e:
            log.warning("postgres not ready (%s: %s); retrying", type(e).__name__, e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=3)
            except TimeoutError:
                pass
    if stop.is_set():
        return

    log.info(
        "score up: interval=%.0fs bin=%s threshold=%.2f window=%s",
        INTERVAL_S,
        BIN,
        GAP_THRESHOLD,
        ACTIVE_WINDOW,
    )
    while not stop.is_set():
        try:
            await scorer.cycle()
        except Exception:
            log.exception("score cycle failed; retrying next interval")
        try:
            await asyncio.wait_for(stop.wait(), timeout=INTERVAL_S)
        except TimeoutError:
            pass
    await db.close()
    log.info("shutdown: scored=%d alerts=%d", scorer.scored, scorer.alerts)


if __name__ == "__main__":
    asyncio.run(run())
