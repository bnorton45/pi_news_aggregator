"""InMemoryDeduper unit tests (PLAN §6.3 step 2). NATS-KV path is exercised by the
docker-compose loop (DEDUP_KV_BUCKET) — it needs a live broker, so not unit-tested here."""

from libs.dedup import InMemoryDeduper, load_deduper


async def test_seen_records_on_first_sight() -> None:
    d = InMemoryDeduper()
    assert await d.seen("a") is False  # first time
    assert await d.seen("a") is True  # now seen
    assert await d.seen("b") is False  # independent key


async def test_fifo_eviction_past_cap() -> None:
    d = InMemoryDeduper(cap=2)
    await d.seen("a")
    await d.seen("b")
    await d.seen("c")  # over cap -> evicts oldest ("a")
    assert await d.seen("a") is False  # "a" was evicted, looks new again


async def test_recent_key_survives_eviction() -> None:
    d = InMemoryDeduper(cap=2)
    await d.seen("a")
    await d.seen("b")
    await d.seen("c")  # evicts "a", keeps {"b","c"}
    assert await d.seen("c") is True


def test_load_deduper_default_is_in_memory() -> None:
    assert isinstance(load_deduper(), InMemoryDeduper)
