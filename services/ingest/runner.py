"""Shared poll loop for pull-based ingesters (PLAN §6.1).

A poll source supplies three pure functions — extract (payload -> raw records),
normalize (raw -> Item | None), dedup_key (raw -> str) — and gets the hardened
loop for free: bounded seen-LRU, publish scoped to its own subject, graceful
shutdown, retry-on-error. Streaming sources (SSE/WS) have their own mains.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

from libs.bus import BusConfig, ScopedPublisher, connect, ensure_stream
from libs.schema import Item
from services.ingest.rss import json_payload

SEEN_CAP = 10_000  # bounded LRU so the dedup set can't grow without limit


@dataclass(frozen=True)
class PollSource:
    name: str  # "usgs", "noaa", ... (also names the log + default UA)
    subject: str  # its own ingest.<name> subject — the only thing it may publish
    feed_url: str
    poll_seconds: float
    extract: Callable[[Any], Iterable[dict[str, Any]]]  # parsed payload -> raw records
    normalize: Callable[[dict[str, Any]], Item | None]
    dedup_key: Callable[[dict[str, Any]], str]
    headers: dict[str, str] = field(default_factory=dict)
    # response -> payload; default decodes JSON. RSS sources pass rss.rss_items.
    parse: Callable[[httpx.Response], Any] = json_payload


def _remember(seen: OrderedDict[str, None], key: str) -> None:
    seen[key] = None
    while len(seen) > SEEN_CAP:
        seen.popitem(last=False)


async def poll_once(
    src: PollSource,
    client: httpx.AsyncClient,
    pub: ScopedPublisher,
    seen: OrderedDict[str, None],
) -> int:
    r = await client.get(src.feed_url)
    r.raise_for_status()
    published = 0
    for raw in src.extract(src.parse(r)):
        key = src.dedup_key(raw)
        if key in seen:
            continue
        item = src.normalize(raw)
        if item is None:
            _remember(seen, key)
            continue
        await pub.publish(src.subject, item)
        _remember(seen, key)
        published += 1
    return published


async def run(src: PollSource) -> None:
    log = logging.getLogger(f"ingest.{src.name}")
    cfg = BusConfig.from_env()
    nc, js = await connect(cfg)
    await ensure_stream(js, cfg)
    pub = ScopedPublisher(js, allowed_prefix=src.subject, max_msg_bytes=cfg.max_msg_bytes)
    seen: OrderedDict[str, None] = OrderedDict()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info(
        "%s ingester up: feed=%s subject=%s poll=%ss",
        src.name,
        src.feed_url,
        src.subject,
        src.poll_seconds,
    )
    headers = {"User-Agent": f"osint-aggregator/0.0 (ingest.{src.name})", **src.headers}
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20.0), headers=headers, follow_redirects=True
    ) as client:
        while not stop.is_set():
            try:
                n = await poll_once(src, client, pub, seen)
                if n:
                    log.info("published %d new %s items", n, src.name)
            except Exception:
                log.exception("poll failed; will retry")
            try:
                await asyncio.wait_for(stop.wait(), timeout=src.poll_seconds)
            except TimeoutError:
                pass

    log.info("shutting down")
    await nc.drain()
