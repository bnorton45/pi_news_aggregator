"""Enrich inference worker (PLAN §6.3, §3.3 inference/db-writer split).

Consumes ingest.* and runs the pure-function inference path — gazetteer tally,
shared dedup, the §6.3a admission governor, and embedding — then publishes a
schema-validated EnrichedItem on enriched.<source>. It holds **NO database
credentials and no DB reachability** (PLAN §3.3): a runtime exploit in this process
(it parses attacker-controlled text) cannot reach Postgres. The db-writer
(services/writer) is the only DB-RW pod; the ENRICHED JetStream buffers between them,
which is also what makes a cold Postgres failover lossless (PLAN §2/§4).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import replace
from datetime import UTC, datetime

from nats.js import JetStreamContext

from libs.bus import BusConfig, ScopedPublisher, connect, consume_validated, ensure_stream
from libs.bus.config import ENRICHED_MAX_MSG_BYTES
from libs.dedup import Deduper, InMemoryDeduper, NatsKvDeduper
from libs.embed import load_embedder
from libs.ner import load_ner
from libs.schema import MAX_TALLY_ENTITIES, EnrichedItem, Item, TallyFlush, merge_entities
from services.enrich.filter import CheapFilter
from services.enrich.governor import AdmissionGovernor

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("enrich")

SUBJECT = os.environ.get("ENRICH_SUBJECT", "ingest.>")
DURABLE = os.environ.get("ENRICH_DURABLE", "enrich")
ENRICHED_PREFIX = "enriched."
TALLY_PREFIX = "tally."
TALLY_FLUSH_S = float(os.environ.get("TALLY_FLUSH_S", "60"))
REPLICA = os.environ.get("HOSTNAME", "enrich-0")  # pod name; tally idempotency key part
# §6.3 step 2 online retrain: watch the models KV for a promoted classifier and hot-swap
# it into the live filter. Unset bucket ⇒ no watch (dev without a retrain loop). We POLL
# the KV rather than kv.watch so enrich needs only KV_models READ grants (get + bind), no
# consumer-create — the smallest grant that reads a model published every few hours.
MODELS_KV_BUCKET = os.environ.get("MODELS_KV_BUCKET")
MODELS_POLL_S = float(os.environ.get("MODELS_POLL_S", "60"))


class _NotReady(Exception):
    """The models bucket doesn't exist yet — a normal pre-first-publish state, retried
    quietly by the watcher rather than logged as a fault."""


async def _build_deduper(js: JetStreamContext, cfg: BusConfig) -> Deduper:
    """Shared NATS-KV dedup when DEDUP_KV_BUCKET is set (cross-replica, 5d TTL — PLAN
    §6.3 step 2); otherwise the per-process in-memory fallback (single-replica)."""
    bucket = os.environ.get("DEDUP_KV_BUCKET")
    if bucket:
        log.info("dedup: NatsKvDeduper bucket=%s ttl=%.0fs", bucket, cfg.max_age_seconds)
        return await NatsKvDeduper.create(js, bucket=bucket, ttl_seconds=cfg.max_age_seconds)
    log.info("dedup: InMemoryDeduper (set DEDUP_KV_BUCKET for the shared store)")
    return InMemoryDeduper()


class Inference:
    def __init__(self, filter_: CheapFilter, publisher: ScopedPublisher) -> None:
        self.filter = filter_
        self.pub = publisher
        self.gov = AdmissionGovernor()
        self.embedder = load_embedder()
        self.ner = load_ner()
        self.published = 0
        self.shed = 0
        self.explored = 0

    async def handle(self, item: Item) -> None:
        self.filter.tally(item)  # gazetteer mentions for velocity — every item, pre-embed (§6.6)
        if await self.filter.is_duplicate(item):
            return
        relevance = self.filter.relevance(item)
        exploration = False
        if not self.gov.admit(relevance=relevance, source_class=item.source_class):
            # §6.3a exploration quota: embed a tiny random sample of the shed tail
            # anyway, tagged — the counterfactuals the filter retrain loop needs.
            if not self.gov.explore():
                self.shed += 1  # governor shedding; gov.sampling_active reflects this
                return
            exploration = True
            self.explored += 1
        merge_entities(item, self.ner.extract(item.text))  # per-survivor NER (PLAN §6.3 step 4)
        vec = self.embedder.encode([item.text])[0]
        enriched = EnrichedItem(item=item, embedding=vec.tolist(), exploration=exploration)
        await self.pub.publish(f"{ENRICHED_PREFIX}{item.source}", enriched)
        self.published += 1
        if self.published % 50 == 0:
            log.info(
                "published=%d shed=%d explored=%d theta=%.3f rate=%.2f/s sampling=%s",
                self.published,
                self.shed,
                self.explored,
                self.gov.theta,
                self.gov.rate_per_s,
                self.gov.sampling_active,
            )

    async def flush_tallies(self, pub: ScopedPublisher, stop: asyncio.Event) -> None:
        """Periodic §6.6 velocity flush: per-minute gazetteer mention deltas + governor
        health, on their own subject so admission sampling can't bias the signal."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=TALLY_FLUSH_S)
            except TimeoutError:
                pass
            counts = self.filter.drain_mentions()
            # Oversize tally (hostile flood of distinct surface forms): keep the top
            # mentions — the velocity signal cares about the hot entities anyway.
            if len(counts) > MAX_TALLY_ENTITIES:
                counts = dict(counts.most_common(MAX_TALLY_ENTITIES))
            now = datetime.now(UTC)
            flush = TallyFlush(
                bucket_ts=now.replace(second=0, microsecond=0),
                counts=dict(counts),
                sampling_active=self.gov.sampling_active,
                theta=self.gov.theta,
                replica=REPLICA,
            )
            try:
                await pub.publish(f"{TALLY_PREFIX}minute", flush)
            except Exception:
                log.exception("tally flush failed; counts window dropped")


async def watch_models(js: JetStreamContext, filter_: CheapFilter, stop: asyncio.Event) -> None:
    """Poll the models KV; install a newly-promoted classifier into the live filter.

    Robust by construction (PLAN §6.3 step 2): a garbage or sha-mismatched artifact is
    logged and skipped — the current model keeps serving, the pipeline never crashes on a
    bad publish. The bucket may not exist until retrain's first cycle, so binding retries.
    """
    if not MODELS_KV_BUCKET:
        return
    import hashlib

    from nats.js.errors import BucketNotFoundError, NotFoundError

    from libs.classify.model_store import ModelStore
    from libs.classify.onnx_classifier import OnnxClassifier

    store: ModelStore | None = None
    last_rev: int | None = None
    while not stop.is_set():
        try:
            if store is None:
                # Expected until retrain's first publish creates the bucket — log softly,
                # no traceback: this is a normal "not ready yet", not a fault.
                try:
                    store = await ModelStore.bind(js, MODELS_KV_BUCKET)
                except (BucketNotFoundError, NotFoundError):
                    log.debug("models KV %s not created yet; will retry", MODELS_KV_BUCKET)
                    raise _NotReady from None
            rev = await store.model_revision()
            if rev is not None and rev != last_rev:
                model = await store.get_model()
                meta = await store.get_meta() or {}
                digest = hashlib.sha256(model).hexdigest()
                if meta.get("version") and meta["version"] != digest:
                    log.warning("models KV meta/model sha mismatch; skipping swap (rev=%d)", rev)
                else:
                    clf = OnnxClassifier(model)
                    # Force one run so a broken graph fails HERE, not on live traffic.
                    clf.score("warmup")
                    filter_.swap_classifier(clf)
                    last_rev = rev
                    log.info("hot-swapped classifier rev=%d v=%s", rev, digest[:12])
        except _NotReady:
            store = None  # bucket absent — quiet retry next tick
        except Exception:
            log.exception("model watch cycle failed; keeping current classifier")
            store = None  # re-bind next tick (connection blip / transient KV error)
        try:
            await asyncio.wait_for(stop.wait(), timeout=MODELS_POLL_S)
        except TimeoutError:
            pass


async def run() -> None:
    cfg = BusConfig.from_env()
    nc, js = await connect(cfg)
    await ensure_stream(js, cfg)  # INGEST (consume)
    enriched_cfg = replace(
        cfg,
        stream=os.environ.get("ENRICHED_STREAM", "ENRICHED"),
        subjects=("enriched.>",),
        max_msg_bytes=ENRICHED_MAX_MSG_BYTES,  # Item cap + embedding headroom
    )
    await ensure_stream(js, enriched_cfg)  # ENRICHED (publish) — the inference/writer seam
    tally_cfg = replace(cfg, stream=os.environ.get("TALLY_STREAM", "TALLY"), subjects=("tally.>",))
    await ensure_stream(js, tally_cfg)  # TALLY (publish) — §6.6 velocity seam
    publisher = ScopedPublisher(js, ENRICHED_PREFIX, enriched_cfg.max_msg_bytes)
    tally_pub = ScopedPublisher(js, TALLY_PREFIX, cfg.max_msg_bytes)
    deduper = await _build_deduper(js, cfg)
    inf = Inference(CheapFilter(deduper=deduper), publisher)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info("enrich(inference) up: in=%s out=%s* durable=%s", SUBJECT, ENRICHED_PREFIX, DURABLE)
    consumer = asyncio.create_task(
        consume_validated(
            js,
            subject=SUBJECT,
            durable=DURABLE,
            stream=cfg.stream,
            model=Item,
            handler=inf.handle,
            max_msg_bytes=cfg.max_msg_bytes,
        )
    )
    flusher = asyncio.create_task(inf.flush_tallies(tally_pub, stop))
    watcher = asyncio.create_task(watch_models(js, inf.filter, stop))
    await stop.wait()
    consumer.cancel()
    await asyncio.gather(consumer, flusher, watcher, return_exceptions=True)
    await nc.drain()
    log.info("shutdown: published=%d shed=%d", inf.published, inf.shed)


if __name__ == "__main__":
    asyncio.run(run())
