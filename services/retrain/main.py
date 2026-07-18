"""Retrain worker (PLAN §6.3 step 2, §4): weak labels → train → eval gate → KV publish.

The online filter-retrain loop. Each cycle pulls the in-window weak labels (as the
read-only `retrain_ro` role), trains a candidate hashing-LR, and promotes it to the
models KV **only if** it clears the eval gate (services/retrain/evalx) — the gate is what
keeps a weak-label poisoning attempt from reaching the live filter. enrich hot-swaps off
the KV; a missed publish just means the previous model keeps serving until the next cycle.

DB writes are exactly one row: the `system_state['retrain']` health beat. Everything
else is read-only (weak_labels) or NATS-KV.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import signal
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from services.retrain.evalx import (
    class_counts,
    evaluate,
    meets_floors,
    should_promote,
    time_split,
)
from services.retrain.train import train_model

if TYPE_CHECKING:  # deferred: keep pure train/eval importable without asyncpg/nats
    from libs.classify.model_store import ModelStore
    from services.retrain.db import RetrainDb

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("retrain")

INTERVAL_S = float(os.environ.get("RETRAIN_INTERVAL_S", "21600"))  # 6h default
MAX_ROWS = int(os.environ.get("RETRAIN_MAX_ROWS", "5000"))  # bound train memory/time
BUCKET = os.environ.get("MODELS_KV_BUCKET", "models")


class Retrainer:
    def __init__(self, db: RetrainDb, store: ModelStore) -> None:
        self.db = db
        self.store = store
        self.cycles = 0
        self.promotions = 0

    async def cycle(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        rows = await self.db.fetch_labels(MAX_ROWS)
        counts = class_counts(rows)
        ok, reason = meets_floors(len(rows), counts)
        promoted = False
        version: str | None = None
        cand_f1: float | None = None
        cur_f1: float | None = None

        if ok:
            train_rows, eval_rows = time_split(rows)
            candidate = train_model([r.text for r in train_rows], [r.label for r in train_rows])
            cand_metrics = evaluate(candidate, eval_rows)
            cand_f1 = cand_metrics["f1"]
            current = await self.store.get_model()
            cur_f1 = evaluate(current, eval_rows)["f1"] if current is not None else None
            promote, reason = should_promote(cand_f1, cur_f1, len(rows), counts)
            if promote:
                version = hashlib.sha256(candidate).hexdigest()
                await self.store.publish(
                    candidate,
                    {
                        "version": version,
                        "metrics": cand_metrics,
                        "n_rows": len(rows),
                        "trained_at": now.isoformat(),
                    },
                )
                promoted = True
                self.promotions += 1
                log.info(
                    "promoted classifier v=%s f1=%.3f (incumbent %s) rows=%d",
                    version[:12],
                    cand_f1,
                    f"{cur_f1:.3f}" if cur_f1 is not None else "none",
                    len(rows),
                )

        self.cycles += 1
        await self.db.upsert_system_state(
            "retrain",
            {
                "last_run": now.isoformat(),
                "labeled_rows": len(rows),
                "class_balance": {"neg": counts.get(0, 0), "pos": counts.get(1, 0)},
                "promoted": promoted,
                "reason": reason,
                "candidate_f1": cand_f1,
                "current_f1": cur_f1,
                "version": version,
                "cycles": self.cycles,
            },
        )
        log.info("cycle: labeled=%d promoted=%s reason=%s", len(rows), promoted, reason)


async def _connect_with_retry(stop: asyncio.Event) -> tuple[RetrainDb, ModelStore] | None:
    """Retry both Postgres and NATS at boot — like score, never crash-exit into a
    CrashLoopBackOff that would stall helm --wait (k3d e2e)."""
    from libs.classify.model_store import ModelStore
    from services.retrain.db import RetrainDb

    db = RetrainDb()
    while not stop.is_set():
        try:
            await db.connect()
            store = await ModelStore.connect(BUCKET)
            return db, store
        except Exception as e:
            log.warning("deps not ready (%s: %s); retrying", type(e).__name__, e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=3)
            except TimeoutError:
                pass
    return None


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    conn = await _connect_with_retry(stop)
    if conn is None:
        return
    db, store = conn
    retrainer = Retrainer(db, store)

    log.info("retrain up: interval=%.0fs bucket=%s max_rows=%d", INTERVAL_S, BUCKET, MAX_ROWS)
    while not stop.is_set():
        try:
            await retrainer.cycle()
        except Exception:
            log.exception("retrain cycle failed; retrying next interval")
        try:
            await asyncio.wait_for(stop.wait(), timeout=INTERVAL_S)
        except TimeoutError:
            pass
    await db.close()
    await store.close()
    log.info("shutdown: cycles=%d promotions=%d", retrainer.cycles, retrainer.promotions)


if __name__ == "__main__":
    asyncio.run(run())
