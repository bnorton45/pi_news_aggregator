"""Hardened Bluesky Jetstream ingester (PLAN §3.1 zone-ingest, §6.1).

Powers: phone home ONLY to the configured jetstream host; NATS *publish only* to
ingest.bluesky; no DB, no other pods. Runs nonroot / read-only-rootfs.

WS consumer: reconnects with cursor=<last time_us> so a blip replays instead of
gaps; replays collapse via the seen-LRU (and the shared KV dedup downstream).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from collections import OrderedDict

import websockets

from libs.bus import BusConfig, ScopedPublisher, connect, ensure_stream
from services.ingest.bluesky.normalize import normalize, should_keep
from services.ingest.runner import SEEN_CAP

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("ingest.bluesky")

JETSTREAM_URL = os.environ.get(
    "BSKY_JETSTREAM_URL",
    "wss://jetstream2.us-east.bsky.network/subscribe?wantedCollections=app.bsky.feed.post",
)
SUBJECT = os.environ.get("NATS_SUBJECT", "ingest.bluesky")
LANGS = frozenset(
    lang.strip().lower() for lang in os.environ.get("BSKY_LANGS", "en").split(",") if lang.strip()
)
RECONNECT_SECONDS = float(os.environ.get("BSKY_RECONNECT_SECONDS", "5"))
LOG_EVERY = 500


def _remember(seen: OrderedDict[str, None], key: str) -> None:
    seen[key] = None
    while len(seen) > SEEN_CAP:
        seen.popitem(last=False)


def _cursor_url(base: str, cursor: int | None) -> str:
    if cursor is None:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}cursor={cursor}"


async def run() -> None:
    cfg = BusConfig.from_env()
    nc, js = await connect(cfg)
    await ensure_stream(js, cfg)
    pub = ScopedPublisher(js, allowed_prefix=SUBJECT, max_msg_bytes=cfg.max_msg_bytes)
    seen: OrderedDict[str, None] = OrderedDict()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info(
        "bluesky ingester up: jetstream=%s subject=%s langs=%s",
        JETSTREAM_URL,
        SUBJECT,
        sorted(LANGS),
    )
    cursor: int | None = None
    published = 0
    while not stop.is_set():
        try:
            async with websockets.connect(
                _cursor_url(JETSTREAM_URL, cursor),
                user_agent_header="osint-aggregator/0.0 (ingest.bluesky)",
            ) as ws:
                while not stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                    try:
                        evt = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(evt, dict) and isinstance(evt.get("time_us"), int):
                        cursor = evt["time_us"]
                    if not should_keep(evt, LANGS):
                        continue
                    item = normalize(evt)
                    if item is None or item.content_hash in seen:
                        continue
                    await pub.publish(SUBJECT, item)
                    _remember(seen, item.content_hash)
                    published += 1
                    if published % LOG_EVERY == 0:
                        log.info("published %d bluesky items (cursor=%s)", published, cursor)
        except Exception:
            if stop.is_set():
                break
            log.exception("jetstream dropped; reconnecting in %ss", RECONNECT_SECONDS)
        try:
            await asyncio.wait_for(stop.wait(), timeout=RECONNECT_SECONDS)
        except TimeoutError:
            pass

    log.info("shutting down (published %d)", published)
    await nc.drain()


if __name__ == "__main__":
    asyncio.run(run())
