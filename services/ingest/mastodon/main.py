"""Hardened Mastodon ingester (PLAN §3.1 zone-ingest, §6.1).

Powers: phone home ONLY to the configured instance set; NATS *publish only* to
ingest.mastodon; no DB, no other pods. Runs nonroot / read-only-rootfs.

Poll-based: each cycle pulls /api/v1/timelines/public per instance with a
per-instance since_id cursor. A failing instance logs and skips — one bad host
must not stall the others.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections import OrderedDict

import httpx

from libs.bus import BusConfig, ScopedPublisher, connect, ensure_stream
from services.ingest.mastodon.normalize import normalize, should_keep
from services.ingest.runner import SEEN_CAP

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("ingest.mastodon")

INSTANCES = [
    inst.strip()
    for inst in os.environ.get("MASTO_INSTANCES", "mstdn.social,mas.to,fosstodon.org").split(",")
    if inst.strip()
]
SUBJECT = os.environ.get("NATS_SUBJECT", "ingest.mastodon")
LANGS = frozenset(
    lang.strip().lower() for lang in os.environ.get("MASTO_LANGS", "en").split(",") if lang.strip()
)
SKIP_BOTS = os.environ.get("MASTO_SKIP_BOTS", "true").lower() == "true"
POLL_SECONDS = float(os.environ.get("MASTO_POLL_SECONDS", "20"))
PAGE_LIMIT = int(os.environ.get("MASTO_PAGE_LIMIT", "40"))


def _remember(seen: OrderedDict[str, None], key: str) -> None:
    seen[key] = None
    while len(seen) > SEEN_CAP:
        seen.popitem(last=False)


async def poll_instance(
    client: httpx.AsyncClient,
    pub: ScopedPublisher,
    seen: OrderedDict[str, None],
    instance: str,
    since_id: str | None,
) -> tuple[int, str | None]:
    """One timeline page for one instance; returns (published, new since_id)."""
    params: dict[str, str | int] = {"limit": PAGE_LIMIT}
    if since_id:
        params["since_id"] = since_id
    r = await client.get(f"https://{instance}/api/v1/timelines/public", params=params)
    r.raise_for_status()
    statuses = r.json()
    if not isinstance(statuses, list) or not statuses:
        return 0, since_id
    # Responses are newest-first; the first id becomes the next cursor.
    newest = str(statuses[0].get("id", "")) or since_id
    published = 0
    for status in statuses:
        if not should_keep(status, LANGS, SKIP_BOTS):
            continue
        item = normalize(status, instance)
        if item is None or item.content_hash in seen:
            continue
        await pub.publish(SUBJECT, item)
        _remember(seen, item.content_hash)
        published += 1
    return published, newest


async def run() -> None:
    cfg = BusConfig.from_env()
    nc, js = await connect(cfg)
    await ensure_stream(js, cfg)
    pub = ScopedPublisher(js, allowed_prefix=SUBJECT, max_msg_bytes=cfg.max_msg_bytes)
    seen: OrderedDict[str, None] = OrderedDict()
    cursors: dict[str, str | None] = dict.fromkeys(INSTANCES)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info(
        "mastodon ingester up: instances=%s subject=%s poll=%ss", INSTANCES, SUBJECT, POLL_SECONDS
    )
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20.0),
        headers={"User-Agent": "osint-aggregator/0.0 (ingest.mastodon)"},
        follow_redirects=True,
    ) as client:
        while not stop.is_set():
            total = 0
            for instance in INSTANCES:
                if stop.is_set():
                    break
                try:
                    n, cursors[instance] = await poll_instance(
                        client, pub, seen, instance, cursors[instance]
                    )
                    total += n
                except Exception:
                    log.exception("instance %s poll failed; skipping this cycle", instance)
            if total:
                log.info("published %d new mastodon items", total)
            try:
                await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
            except TimeoutError:
                pass

    log.info("shutting down")
    await nc.drain()


if __name__ == "__main__":
    asyncio.run(run())
