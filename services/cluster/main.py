"""Cluster service (PLAN §6.4): online clustering of enriched Items into Stories.

Consumes enriched.* (its own durable, independent of the db-writer), runs a
partition-pruned pgvector ANN to find the nearest already-clustered neighbours, and
either assigns the item to an existing Story or opens a new one. For *candidate*
Stories (≥2 independent origins) it publishes a ClaimRequest on `llm.heavy` — the only
path by which the local claim LLM (qwen3:1.7b) ever sees survivor text (PLAN §6.3 tiering).

Risk posture: like the db-writer this is a structured-data pod (vector + entities +
parameterized SQL), not a text-executing one — the LLM lives in claimx (no DB).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import numpy as np

from libs.bus import BusConfig, ScopedPublisher, connect, consume_validated, ensure_stream
from libs.bus.config import ENRICHED_MAX_MSG_BYTES
from libs.dedup.simhash import simhash64, to_signed64
from libs.schema import ClaimRequest, EnrichedItem
from libs.trust import (
    EdgeType,
    ProvEdge,
    TrustState,
    detect_edges,
    distinct_origin_sources,
    independent_origins,
    next_state,
    wire_ref,
)
from services.cluster.cluster import (
    Neighbor,
    choose_story,
    entity_texts,
    in_story_primary_match,
    is_candidate,
    prov_node,
)
from services.cluster.db import ClusterDb

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("cluster")

SUBJECT = os.environ.get("CLUSTER_SUBJECT", "enriched.>")
DURABLE = os.environ.get("CLUSTER_DURABLE", "cluster")
LLM_PREFIX = "llm."
THETA = float(os.environ.get("CLUSTER_THETA", "0.82"))  # cosine merge threshold
WINDOW_DAYS = int(os.environ.get("CLUSTER_WINDOW_DAYS", "2"))  # ANN partition prune
TOPK = int(os.environ.get("CLUSTER_TOPK", "10"))
CORROBORATION_N = int(os.environ.get("CORROBORATION_N", "3"))  # §6.5 default
# §6.5 source-diversity floor: distinct feeds/platforms an origin set must span before
# CORROBORATED (blocks single-platform amplification).
CORROBORATION_SOURCES = int(os.environ.get("CORROBORATION_SOURCES", "2"))


def _neighbors(rows: list) -> list[Neighbor]:
    out: list[Neighbor] = []
    for r in rows:
        ents = {e.get("text", "").casefold() for e in json.loads(r["entities"])}
        out.append(
            Neighbor(story_id=r["story_id"], similarity=r["sim"], entity_texts=frozenset(ents))
        )
    return out


class Clusterer:
    def __init__(self, db: ClusterDb, publisher: ScopedPublisher) -> None:
        self.db = db
        self.pub = publisher
        self.assigned = 0
        self.new_stories = 0
        self.claims = 0

    async def handle(self, enriched: EnrichedItem) -> None:
        item = enriched.item
        vec = np.asarray(enriched.embedding, dtype=np.float32)
        new_ents = entity_texts(item.entities)
        since = datetime.now(UTC) - timedelta(days=WINDOW_DAYS)

        rows = await self.db.nearest(vec, since, item.id, TOPK)
        target = choose_story(new_ents, _neighbors(rows), THETA)
        story_id = target or uuid4()

        # Assign FIRST: if the writer hasn't stored the item yet, retry before writing any
        # Story row, so a NAK can't leave an orphan Story behind. wire_ref is computed
        # here alongside simhash — same text, same layer (§6.5 syndication collapse).
        sim = to_signed64(simhash64(item.text))
        wire = wire_ref(item.text)
        if not await self.db.assign_item(item.id, item.ts_observed, story_id, sim, wire):
            raise RuntimeError(f"item {item.id} not stored yet; nak for redelivery")

        if target is None:
            await self.db.create_story(story_id, item.ts_observed, list(new_ents), item.source, vec)
            self.new_stories += 1
            origins = 1
        else:
            await self.db.touch_story(story_id, item.ts_observed, item.source, list(new_ents))
            origins = await self._update_trust(story_id, item.id)
        self.assigned += 1

        if is_candidate(origins):  # PLAN §6.3 llm.heavy gate — candidate Stories only
            req = ClaimRequest(story_id=story_id, item_id=item.id, text=item.text)
            await self.pub.publish(f"{LLM_PREFIX}heavy", req)
            self.claims += 1

        if self.assigned % 50 == 0:
            log.info(
                "assigned=%d new_stories=%d claims_queued=%d",
                self.assigned,
                self.new_stories,
                self.claims,
            )

    async def _update_trust(self, story_id: UUID, new_item_id: UUID) -> int:
        """Provenance + trust for a Story that just gained a member (PLAN §6.5):
        detect edges from the new member, persist them, recount independent origins
        (WCC deduped by org/domain), and promote the trust state monotonically."""
        rows = await self.db.story_prov(story_id)
        nodes = [prov_node(r) for r in rows]
        by_id = {n.item_id: n for n in nodes}
        new_node = by_id.get(new_item_id)
        if new_node is None:  # not visible yet — evidence recounts on the next member
            return len(nodes)

        found = detect_edges(new_node, nodes)
        await self.db.insert_edges(
            story_id,
            [(e.src, e.dst, e.edge_type.value) for e in found],
            next(r["ts_observed"] for r in rows if r["id"] == new_item_id),
        )
        stored = await self.db.story_edges(story_id)
        edges = [ProvEdge(r["src_item"], r["dst_item"], EdgeType(r["edge_type"])) for r in stored]
        origins = independent_origins(nodes, edges)
        n_sources = distinct_origin_sources(nodes, edges)

        classes = {r["source_class"] for r in rows}
        state = next_state(
            TrustState(await self.db.story_state(story_id)),
            origins,
            distinct_sources=n_sources,
            primary_matched=in_story_primary_match(classes),
            n=CORROBORATION_N,
            min_sources=CORROBORATION_SOURCES,
        )
        await self.db.update_trust(story_id, origins, state.value)
        return origins


async def run() -> None:
    # Match the enrich/writer ENRICHED stream config (cap incl. embedding headroom) so
    # ensure_stream reconciles instead of shrinking it (PLAN §3.3 seam).
    cfg = replace(
        BusConfig.from_env(),
        stream=os.environ.get("ENRICHED_STREAM", "ENRICHED"),
        subjects=("enriched.>",),
        max_msg_bytes=ENRICHED_MAX_MSG_BYTES,
    )
    db = ClusterDb()
    await db.connect()
    nc, js = await connect(cfg)
    await ensure_stream(js, cfg)  # ENRICHED (consume)
    llm_cfg = replace(cfg, stream=os.environ.get("LLM_STREAM", "LLM_HEAVY"), subjects=("llm.>",))
    await ensure_stream(js, llm_cfg)  # LLM_HEAVY (publish) — the gated claim-LLM queue
    publisher = ScopedPublisher(js, LLM_PREFIX, cfg.max_msg_bytes)
    clusterer = Clusterer(db, publisher)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info(
        "cluster up: in=%s out=%sheavy durable=%s theta=%.2f", SUBJECT, LLM_PREFIX, DURABLE, THETA
    )
    consumer = asyncio.create_task(
        consume_validated(
            js,
            subject=SUBJECT,
            durable=DURABLE,
            stream=cfg.stream,
            model=EnrichedItem,
            handler=clusterer.handle,
            max_msg_bytes=cfg.max_msg_bytes,
        )
    )
    await stop.wait()
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)
    await nc.drain()
    await db.close()
    log.info(
        "shutdown: assigned=%d new_stories=%d claims_queued=%d",
        clusterer.assigned,
        clusterer.new_stories,
        clusterer.claims,
    )


if __name__ == "__main__":
    asyncio.run(run())
