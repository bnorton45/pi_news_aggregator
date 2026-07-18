"""Hardened Wikipedia EventStreams ingester (PLAN §3.1 zone-ingest, §6.1).

Powers: phone home ONLY to stream.wikimedia.org; NATS *publish only* to
ingest.wikipedia; no DB, no other pods. Runs nonroot / read-only-rootfs.

SSE consumer: reconnects with Last-Event-ID so a blip replays instead of gaps;
replayed events are dropped by the seen-LRU (and the shared KV dedup downstream).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any

import httpx

from libs.bus import BusConfig, ScopedPublisher, connect, ensure_stream
from services.ingest.runner import SEEN_CAP
from services.ingest.wikipedia.normalize import normalize, should_keep

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("ingest.wikipedia")

STREAM_URL = os.environ.get(
    "WIKI_STREAM_URL", "https://stream.wikimedia.org/v2/stream/recentchange"
)
SUBJECT = os.environ.get("NATS_SUBJECT", "ingest.wikipedia")
DOMAINS = frozenset(
    d.strip() for d in os.environ.get("WIKI_DOMAINS", "en.wikipedia.org").split(",") if d.strip()
)
SKIP_MINOR = os.environ.get("WIKI_SKIP_MINOR", "false").lower() == "true"
RECONNECT_SECONDS = float(os.environ.get("WIKI_RECONNECT_SECONDS", "5"))
LOG_EVERY = 100


async def sse_events(
    client: httpx.AsyncClient, url: str, last_id: str | None
) -> AsyncIterator[tuple[str | None, dict[str, Any]]]:
    """Yield (event_id, parsed_data) per SSE message; ends when the server drops us."""
    headers = {"Accept": "text/event-stream"}
    if last_id:
        headers["Last-Event-ID"] = last_id
    async with client.stream("GET", url, headers=headers) as r:
        r.raise_for_status()
        event_id: str | None = None
        data_lines: list[str] = []
        async for line in r.aiter_lines():
            if line.startswith("id:"):
                event_id = line[3:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif line == "" and data_lines:
                try:
                    evt = json.loads("\n".join(data_lines))
                except ValueError:
                    evt = None
                data_lines = []
                if isinstance(evt, dict):
                    yield event_id, evt


def _remember(seen: OrderedDict[str, None], key: str) -> None:
    seen[key] = None
    while len(seen) > SEEN_CAP:
        seen.popitem(last=False)


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
        "wikipedia ingester up: stream=%s subject=%s domains=%s",
        STREAM_URL,
        SUBJECT,
        sorted(DOMAINS),
    )
    last_id: str | None = None
    published = 0
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=None),  # SSE: no read timeout between events
        headers={"User-Agent": "osint-aggregator/0.0 (ingest.wikipedia)"},
        follow_redirects=True,
    ) as client:
        while not stop.is_set():
            try:
                async for event_id, evt in sse_events(client, STREAM_URL, last_id):
                    if event_id:
                        last_id = event_id
                    if stop.is_set():
                        break
                    if not should_keep(evt, DOMAINS, SKIP_MINOR):
                        continue
                    item = normalize(evt)
                    if item is None or item.content_hash in seen:
                        continue
                    await pub.publish(SUBJECT, item)
                    _remember(seen, item.content_hash)
                    published += 1
                    if published % LOG_EVERY == 0:
                        log.info("published %d wikipedia items", published)
            except Exception:
                log.exception("stream dropped; reconnecting in %ss", RECONNECT_SECONDS)
            try:
                await asyncio.wait_for(stop.wait(), timeout=RECONNECT_SECONDS)
            except TimeoutError:
                pass

    log.info("shutting down (published %d)", published)
    await nc.drain()


if __name__ == "__main__":
    asyncio.run(run())
