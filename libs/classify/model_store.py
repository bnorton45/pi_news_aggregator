"""Model artifact transport over NATS JetStream KV (PLAN §6.3 step 2, §4).

The retrain loop publishes the trained ONNX bytes here; enrich reads them to hot-swap.
KV, not object storage, because the artifact is tiny (a hashed-LR graph, ~64 KB) and
lives inside the 5-day-wall infra already on the cluster — a future encoder model would
outgrow this and move to object storage under the same reader/writer contract.

Two keys, published meta-then-model so a reader keying on the model never sees a meta
that disagrees with it:
  * ``classifier``       — the raw ONNX bytes.
  * ``classifier.meta``  — JSON {version=sha256(model), metrics, n_rows, trained_at}.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nats.aio.client import Client as NATS
    from nats.js import JetStreamContext
    from nats.js.kv import KeyValue

log = logging.getLogger("model-store")

MODEL_KEY = "classifier"
META_KEY = "classifier.meta"


class ModelStore:
    def __init__(self, kv: KeyValue, nc: NATS | None = None) -> None:
        self._kv = kv
        self._nc = nc  # owned connection to drain on close; None when bound to a shared js

    @classmethod
    async def connect(cls, bucket: str) -> ModelStore:
        """Open a dedicated NATS connection (BusConfig from env) and create-or-bind the
        bucket. Used by the retrain writer, which holds `$JS.API.STREAM.CREATE.KV_models`."""
        from nats.js.api import KeyValueConfig

        from libs.bus import BusConfig, connect

        nc, js = await connect(BusConfig.from_env())
        try:
            kv = await js.create_key_value(config=KeyValueConfig(bucket=bucket, history=1))
        except Exception:  # bucket already exists → bind to it
            kv = await js.key_value(bucket)
        return cls(kv, nc=nc)

    @classmethod
    async def bind(cls, js: JetStreamContext, bucket: str) -> ModelStore:
        """Bind (read-only) to an existing bucket over a shared JetStream context. Used by
        enrich, which has only KV_models READ grants — raises if retrain hasn't created
        the bucket yet, so the caller retries."""
        return cls(await js.key_value(bucket))

    async def _get(self, key: str) -> tuple[bytes, int] | None:
        """(value, revision) or None if the key is unset."""
        from nats.js.errors import KeyNotFoundError

        try:
            entry = await self._kv.get(key)
        except KeyNotFoundError:
            return None
        return entry.value, entry.revision

    async def get_model(self) -> bytes | None:
        got = await self._get(MODEL_KEY)
        return got[0] if got else None

    async def model_revision(self) -> int | None:
        got = await self._get(MODEL_KEY)
        return got[1] if got else None

    async def get_meta(self) -> dict | None:
        got = await self._get(META_KEY)
        return json.loads(got[0]) if got else None

    async def publish(self, model_bytes: bytes, meta: dict) -> None:
        await self._kv.put(META_KEY, json.dumps(meta).encode())
        await self._kv.put(MODEL_KEY, model_bytes)

    async def close(self) -> None:
        if self._nc is not None:  # only when we own the connection (connect, not bind)
            await self._nc.drain()
