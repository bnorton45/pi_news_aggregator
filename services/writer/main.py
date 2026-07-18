"""DB-writer service (PLAN §3.1/§3.3 inference/db-writer split).

The ONLY pod in zone-process with database write credentials. Consumes the
schema-validated EnrichedItem stream (enriched.*) — never raw attacker text — and
writes Item + embedding to partitioned pgvector. A runtime exploit in an inference
worker therefore cannot reach Postgres (PLAN §3.3). ON CONFLICT DO NOTHING keeps
redelivery idempotent; the ENRICHED JetStream buffers writes across a cold Postgres
failover, making it lossless (PLAN §2/§4).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import replace

import numpy as np

from libs.bus import BusConfig, connect, consume_validated, ensure_stream
from libs.bus.config import ENRICHED_MAX_MSG_BYTES
from libs.schema import EnrichedItem, TallyFlush
from services.enrich.db import Db

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("writer")

SUBJECT = os.environ.get("WRITER_SUBJECT", "enriched.>")
DURABLE = os.environ.get("WRITER_DURABLE", "writer")
TALLY_SUBJECT = os.environ.get("WRITER_TALLY_SUBJECT", "tally.>")
TALLY_DURABLE = os.environ.get("WRITER_TALLY_DURABLE", "writer-tally")


class Writer:
    def __init__(self, db: Db) -> None:
        self.db = db
        self.stored = 0
        self.tallies = 0

    async def handle(self, enriched: EnrichedItem) -> None:
        vec = np.asarray(enriched.embedding, dtype=np.float32)
        await self.db.insert_item(enriched.item, vec, exploration=enriched.exploration)
        self.stored += 1
        if self.stored % 50 == 0:
            log.info("stored=%d", self.stored)

    async def handle_tally(self, flush: TallyFlush) -> None:
        """§6.6 velocity persistence + §6.8 governor health beat, one message each
        minute per enrich replica. Both writes are idempotent under redelivery."""
        await self.db.insert_tallies(flush)
        await self.db.upsert_system_state(
            f"enrich:{flush.replica}",
            {
                "sampling_active": flush.sampling_active,
                "theta": flush.theta,
                "flushed_at": flush.flushed_at.isoformat(),
            },
        )
        self.tallies += 1
        if self.tallies % 50 == 0:
            log.info("tally flushes=%d", self.tallies)


async def run() -> None:
    base = BusConfig.from_env()
    cfg = replace(
        base,
        stream=os.environ.get("ENRICHED_STREAM", "ENRICHED"),
        subjects=("enriched.>",),
        max_msg_bytes=ENRICHED_MAX_MSG_BYTES,  # match the inference publisher (PLAN §3.3)
    )
    # TALLY keeps the default boundary cap — matches the enrich tally publisher (§6.6).
    tally_cfg = replace(base, stream=os.environ.get("TALLY_STREAM", "TALLY"), subjects=("tally.>",))
    db = Db()
    await db.connect()
    nc, js = await connect(cfg)
    await ensure_stream(js, cfg)
    await ensure_stream(js, tally_cfg)
    writer = Writer(db)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info(
        "writer up: subject=%s durable=%s tally=%s/%s",
        SUBJECT,
        DURABLE,
        TALLY_SUBJECT,
        TALLY_DURABLE,
    )
    consumer = asyncio.create_task(
        consume_validated(
            js,
            subject=SUBJECT,
            durable=DURABLE,
            stream=cfg.stream,
            model=EnrichedItem,
            handler=writer.handle,
            max_msg_bytes=cfg.max_msg_bytes,
        )
    )
    tally_consumer = asyncio.create_task(
        consume_validated(
            js,
            subject=TALLY_SUBJECT,
            durable=TALLY_DURABLE,
            stream=tally_cfg.stream,
            model=TallyFlush,
            handler=writer.handle_tally,
            max_msg_bytes=tally_cfg.max_msg_bytes,
        )
    )
    await stop.wait()
    consumer.cancel()
    tally_consumer.cancel()
    await asyncio.gather(consumer, tally_consumer, return_exceptions=True)
    await nc.drain()
    await db.close()
    log.info("shutdown: stored=%d tallies=%d", writer.stored, writer.tallies)


if __name__ == "__main__":
    asyncio.run(run())
