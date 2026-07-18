"""NATS JetStream helpers: account-scoped publish + boundary-validated consume.

Two structural guarantees from PLAN §3.2:
  * A `ScopedPublisher` can only publish under its allowed subject prefix — defense
    in depth on top of the broker-enforced NATS account ACL.
  * Every consumed message is size-capped and Pydantic-validated *before* the
    handler sees it; violations are dropped (`term`-ed), never retried.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

import nats
from nats.aio.client import Client as NATS
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js import JetStreamContext
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DiscardPolicy,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from pydantic import BaseModel, ValidationError

from libs.bus.config import BusConfig

log = logging.getLogger("bus")

M = TypeVar("M", bound=BaseModel)

NAK_DELAY_SECONDS = 1.0  # redelivery backoff after a handler error (see consume_validated)


async def connect(cfg: BusConfig) -> tuple[NATS, JetStreamContext]:
    nc = await nats.connect(
        cfg.url,
        user_credentials=cfg.creds,  # None on dev; account .creds in-cluster
        max_reconnect_attempts=-1,
        name=cfg.stream.lower(),
    )
    return nc, nc.jetstream()


async def ensure_stream(js: JetStreamContext, cfg: BusConfig) -> None:
    """Declare the JetStream stream with the 5-day wall (PLAN §4). Idempotent."""
    sc = StreamConfig(
        name=cfg.stream,
        subjects=list(cfg.subjects),
        max_age=cfg.max_age_seconds,  # auto-expire — the hard data wall
        max_msg_size=cfg.max_msg_bytes,  # broker-side size cap
        storage=StorageType.FILE,
        retention=RetentionPolicy.LIMITS,
        discard=DiscardPolicy.OLD,
    )
    try:
        await js.add_stream(sc)
    except Exception:  # already exists → reconcile config
        await js.update_stream(sc)


class ScopedPublisher:
    """Publishes only under `allowed_prefix`. Enforces the boundary size cap."""

    def __init__(self, js: JetStreamContext, allowed_prefix: str, max_msg_bytes: int) -> None:
        self._js = js
        self._prefix = allowed_prefix
        self._max = max_msg_bytes

    async def publish(self, subject: str, model: BaseModel) -> None:
        if not subject.startswith(self._prefix):
            raise PermissionError(f"subject {subject!r} outside allowed prefix {self._prefix!r}")
        data = model.model_dump_json().encode()
        if len(data) > self._max:
            raise ValueError(f"payload {len(data)}B exceeds cap {self._max}B")
        await self._js.publish(subject, data)


async def consume_validated(
    js: JetStreamContext,
    *,
    subject: str,
    durable: str,
    stream: str,
    model: type[M],
    handler: Callable[[M], Awaitable[None]],
    max_msg_bytes: int,
    batch: int = 64,
    max_ack_pending: int = 256,
) -> None:
    """Pull-consume `subject`, validating each message into `model` before `handler`.

    Pull + bounded `max_ack_pending` gives natural backpressure for the enrich
    governor (PLAN §6.3a). Malformed / oversized messages are dropped (`term`).
    Runs forever; cancel the task to stop.

    `stream` must be explicit: without it nats-py resolves the stream via
    `$JS.API.STREAM.NAMES`, which the per-user broker allow-lists deny by design
    (deploy/policies/nats-accounts.conf — caught live by the k3d e2e).
    """
    psub = await js.pull_subscribe(
        subject,
        durable=durable,
        stream=stream,
        config=ConsumerConfig(ack_policy=AckPolicy.EXPLICIT, max_ack_pending=max_ack_pending),
    )
    while True:
        try:
            msgs = await psub.fetch(batch, timeout=5)
        except (TimeoutError, NatsTimeoutError):
            continue  # no messages ready within the window — normal, keep polling
        except Exception:
            log.exception("fetch error on %s; backing off", subject)
            await asyncio.sleep(1)
            continue
        for msg in msgs:
            if len(msg.data) > max_msg_bytes:
                log.warning("drop oversized msg on %s (%dB)", subject, len(msg.data))
                await msg.term()
                continue
            try:
                parsed = model.model_validate_json(msg.data)
            except ValidationError as e:
                log.warning("drop invalid msg on %s: %s", subject, e.errors(include_url=False))
                await msg.term()  # reject-and-drop, never retry a poison message
                continue
            try:
                await handler(parsed)
            except Exception:
                log.exception("handler error on %s; nak for redelivery", subject)
                # Delayed nak: immediate redelivery turns a persistent handler error
                # (e.g. cluster waiting on the db-writer, PG failover §6.8) into a hot loop.
                await msg.nak(delay=NAK_DELAY_SECONDS)
                continue
            await msg.ack()
