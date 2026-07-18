"""Trust worker (PLAN §6.5): promote Stories to PRIMARY_BACKED off extracted claims.

Consumes ClaimResult on `claim.*` (published by claimx after the gated claim LLM
isolates a checkable assertion), aligns the claiming item's entities/geo/time against
primary & authoritative records in the window, and on a match promotes the Story.
Complements the cluster service's cheap in-story promotion (same §6.5 rule, for
records that already clustered into the Story); this path catches the records
that did NOT cluster with the claim — different wording, same event.

Powers: NATS *consume-only* on the CLAIM stream (no app-subject publish at all) +
parameterized SQL. No model, no egress.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from dataclasses import replace
from datetime import timedelta

from libs.bus import BusConfig, connect, consume_validated, ensure_stream
from libs.schema import ClaimResult
from services.trust.db import TrustDb
from services.trust.match import MatchCandidate, find_primary_match

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("trust")

SUBJECT = os.environ.get("TRUST_SUBJECT", "claim.>")
DURABLE = os.environ.get("TRUST_DURABLE", "trust")
WINDOW = timedelta(hours=float(os.environ.get("TRUST_MATCH_WINDOW_H", "24")))
CANDIDATE_LIMIT = int(os.environ.get("TRUST_CANDIDATE_LIMIT", "50"))


def _entity_texts(entities_json: str) -> list[str]:
    return sorted(
        {e.get("text", "").casefold() for e in json.loads(entities_json)} - {""},
    )


def _latlon(geo_json: str | None) -> tuple[float | None, float | None]:
    if not geo_json:
        return None, None
    g = json.loads(geo_json)
    return g.get("lat"), g.get("lon")


class PrimaryMatcher:
    def __init__(self, db: TrustDb) -> None:
        self.db = db
        self.matched = 0
        self.seen = 0

    async def handle(self, claim: ClaimResult) -> None:
        self.seen += 1
        if not claim.claim.strip():  # claim LLM found no checkable assertion
            return
        item = await self.db.claim_item(claim.item_id)
        if item is None or item["story_id"] != claim.story_id:
            return  # aged out, or story reassigned since extraction
        if item["source_class"] != "social":
            return  # §6.5: promotion is for *social* claims; records don't back records

        entities = _entity_texts(item["entities"])
        if not entities:
            return
        rows = await self.db.primary_candidates(
            item["ts_observed"], WINDOW, entities, CANDIDATE_LIMIT
        )
        lat, lon = _latlon(item["geo"])
        candidates = [
            MatchCandidate(
                item_id=r["id"],
                entity_texts=frozenset(_entity_texts(r["entities"])),
                lat=_latlon(r["geo"])[0],
                lon=_latlon(r["geo"])[1],
            )
            for r in rows
        ]
        record = find_primary_match(set(entities), lat, lon, candidates)
        if record is not None:
            await self.db.promote_primary_backed(claim.story_id)
            self.matched += 1
            log.info(
                "primary_backed: story=%s claim_item=%s record=%s (matched=%d/%d)",
                claim.story_id,
                claim.item_id,
                record,
                self.matched,
                self.seen,
            )


async def run() -> None:
    cfg = replace(
        BusConfig.from_env(),
        stream=os.environ.get("CLAIM_STREAM", "CLAIM"),
        subjects=("claim.>",),
    )
    db = TrustDb()
    await db.connect()
    nc, js = await connect(cfg)
    await ensure_stream(js, cfg)  # CLAIM (consume)
    matcher = PrimaryMatcher(db)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info("trust up: in=%s durable=%s window=%s", SUBJECT, DURABLE, WINDOW)
    consumer = asyncio.create_task(
        consume_validated(
            js,
            subject=SUBJECT,
            durable=DURABLE,
            stream=cfg.stream,
            model=ClaimResult,
            handler=matcher.handle,
            max_msg_bytes=cfg.max_msg_bytes,
        )
    )
    await stop.wait()
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)
    await nc.drain()
    await db.close()
    log.info("shutdown: matched=%d seen=%d", matcher.matched, matcher.seen)


if __name__ == "__main__":
    asyncio.run(run())
