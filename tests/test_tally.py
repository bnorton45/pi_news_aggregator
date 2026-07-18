"""Tally pipeline tests (PLAN §6.6): flush schema caps, delta semantics, flush loop."""

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from libs.schema import MAX_TALLY_ENTITIES, Item, SourceClass, TallyFlush
from libs.schema.tally import MAX_TALLY_KEY_LEN
from services.enrich.filter import CheapFilter
from services.enrich.main import Inference

NOW = datetime(2026, 7, 5, 12, 34, 56, 789000, tzinfo=UTC)


class _CapturePublisher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, TallyFlush]] = []

    async def publish(self, subject: str, model: TallyFlush) -> None:
        self.sent.append((subject, model))


def _social(text: str, content_hash: str) -> Item:
    return Item(
        source="bluesky", source_class=SourceClass.SOCIAL, text=text, content_hash=content_hash
    )


# ── TallyFlush boundary caps ──────────────────────────────────────────────────


def test_flush_roundtrip() -> None:
    f = TallyFlush(bucket_ts=NOW, counts={"tokyo": 3}, theta=0.5, replica="enrich-0")
    back = TallyFlush.model_validate_json(f.model_dump_json())
    assert back.counts == {"tokyo": 3}
    assert back.replica == "enrich-0"


def test_flush_rejects_too_many_entities() -> None:
    counts = {f"e{i}": 1 for i in range(MAX_TALLY_ENTITIES + 1)}
    with pytest.raises(ValidationError):
        TallyFlush(bucket_ts=NOW, counts=counts)


def test_flush_rejects_bad_keys_and_counts() -> None:
    for counts in ({"": 1}, {"k" * (MAX_TALLY_KEY_LEN + 1): 1}, {"ok": -1}):
        with pytest.raises(ValidationError):
            TallyFlush(bucket_ts=NOW, counts=counts)


def test_flush_rejects_theta_out_of_bounds() -> None:
    for theta in (-0.1, 1.1):
        with pytest.raises(ValidationError):
            TallyFlush(bucket_ts=NOW, theta=theta)


# ── drain_mentions delta semantics ────────────────────────────────────────────


def test_drain_mentions_is_a_delta() -> None:
    f = CheapFilter()
    f.tally(_social("Explosion reported in Tokyo tonight", "h1"))
    f.tally(_social("Tokyo blast follow-up", "h2"))
    first = f.drain_mentions()
    assert sum(first.values()) > 0  # gazetteer saw the mentions
    assert f.drain_mentions() == {}  # window handed off — starts empty
    f.tally(_social("Explosion reported in Tokyo tonight", "h1"))
    second = f.drain_mentions()
    assert sum(second.values()) > 0  # new window accumulates fresh, not cumulative


# ── flush_tallies loop: bucket flooring + oversize cap ───────────────────────


async def _run_one_flush(inf: Inference, pub: _CapturePublisher) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(inf.flush_tallies(pub, stop))
    await asyncio.sleep(0.05)  # let the loop reach its wait
    stop.set()  # wakes the wait → one final flush, then exit
    await asyncio.wait_for(task, timeout=5)


async def test_flush_loop_floors_bucket_and_drains() -> None:
    pub = _CapturePublisher()
    inf = Inference(CheapFilter(), pub)  # type: ignore[arg-type]
    inf.filter.tally(_social("Explosion reported in Tokyo tonight", "h1"))
    await _run_one_flush(inf, pub)
    assert len(pub.sent) == 1
    subject, flush = pub.sent[0]
    assert subject == "tally.minute"
    assert flush.bucket_ts.second == 0 and flush.bucket_ts.microsecond == 0
    assert sum(flush.counts.values()) > 0
    assert inf.filter.mentions == {}  # drained — next window is a fresh delta


async def test_flush_loop_caps_hostile_entity_flood() -> None:
    pub = _CapturePublisher()
    inf = Inference(CheapFilter(), pub)  # type: ignore[arg-type]
    inf.filter.mentions.update({f"flood-{i}": 1 for i in range(MAX_TALLY_ENTITIES + 100)})
    inf.filter.mentions["hot-entity"] = 50
    await _run_one_flush(inf, pub)
    _, flush = pub.sent[0]
    assert len(flush.counts) == MAX_TALLY_ENTITIES
    assert flush.counts["hot-entity"] == 50  # most_common keeps the hot entities
