"""Dedup store (PLAN §6.3 step 2, §6.5).

Exact-dedup (`content_hash`) + near-dup (simhash/LSH) of the firehose: collapse
copypasta to a single embedded representative, while independent origins are still
counted from metadata downstream (§6.5).

Production path: a SHARED store (NATS JetStream KV) with a 5-day TTL — correct across
the two classifier replicas and unable to match content older than the wall (§4). The
atomic primitive is *create-if-absent*: a key that already exists means "seen". Near-dup
(simhash + LSH banding) is the next layer (PLAN §6.3 step 2) and not implemented yet.
Dev/0a fallback: a per-process in-memory LRU (NON-shared, single-replica, exact-only).
Callers depend only on the async `Deduper` Protocol; pick an impl via env (the enrich
service builds `NatsKvDeduper` when ``DEDUP_KV_BUCKET`` is set, else in-memory).
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from nats.js import JetStreamContext
    from nats.js.kv import KeyValue

log = logging.getLogger("dedup")


class Deduper(Protocol):
    async def seen(self, key: str) -> bool:
        """Return True if `key` was already seen; else record it and return False."""
        ...


class InMemoryDeduper:
    """Per-process exact-dedup LRU. NON-shared — dev/0a only (PLAN §6.3 step 2).

    Eviction is FIFO (oldest inserted first) once `cap` is exceeded. Near-dup
    (simhash/LSH) is a production-store concern and is not attempted here.
    """

    def __init__(self, cap: int = 50_000) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._cap = cap

    async def seen(self, key: str) -> bool:
        if key in self._seen:
            return True
        self._seen[key] = None
        while len(self._seen) > self._cap:
            self._seen.popitem(last=False)
        return False


def _safe_key(key: str) -> str:
    # NATS KV keys allow only [-/_=.a-zA-Z0-9]; hash to a safe, bounded form.
    return hashlib.sha256(key.encode()).hexdigest()


class NatsKvDeduper:
    """Shared exact-dedup over NATS JetStream KV (PLAN §6.3 step 2).

    Correct across replicas and TTL'd to the 5-day wall (§4). `seen` uses an atomic
    create (compare-and-set against revision 0): success => first sight; an
    already-exists error => duplicate. Unexpected KV errors **fail open** (treat as
    new) so a KV blip degrades to occasional duplicates rather than halting ingest.
    """

    def __init__(self, kv: KeyValue) -> None:
        self._kv = kv

    @classmethod
    async def create(
        cls, js: JetStreamContext, *, bucket: str, ttl_seconds: float
    ) -> NatsKvDeduper:
        from nats.js.api import KeyValueConfig

        cfg = KeyValueConfig(bucket=bucket, ttl=ttl_seconds, history=1)
        try:
            kv = await js.create_key_value(config=cfg)
        except Exception:  # bucket already exists -> bind to it
            kv = await js.key_value(bucket)
        return cls(kv)

    async def seen(self, key: str) -> bool:
        from nats.js.errors import KeyWrongLastSequenceError

        try:
            await self._kv.create(_safe_key(key), b"1")
            return False
        except KeyWrongLastSequenceError:
            return True
        except Exception:
            log.exception("KV dedup error; failing open (treat as new)")
            return False


def load_deduper() -> Deduper:
    """Default (no infra): in-memory. The shared NATS-KV store is built explicitly by
    the enrich service via ``NatsKvDeduper.create`` when ``DEDUP_KV_BUCKET`` is set."""
    return InMemoryDeduper()
