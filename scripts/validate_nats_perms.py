"""Live-broker validation of deploy/policies/nats-accounts.conf per-user permissions.

Exercises each service user's REAL operation set (streams ensured, durables, acks,
KV dedup) and asserts a few key denials. Run against a broker started from the
authored config."""

import asyncio

import nats
from nats.errors import TimeoutError as NatsTimeout
from nats.js.api import StreamConfig
from nats.js.errors import KeyWrongLastSequenceError

URL = "nats://{u}:x@127.0.0.1:4222"
OK: list[str] = []


def ok(msg: str) -> None:
    OK.append(msg)
    print(f"  PASS  {msg}")


async def denied(coro, what: str) -> None:
    try:
        await asyncio.wait_for(coro, timeout=3)
    except (TimeoutError, NatsTimeout):
        ok(f"DENIED as expected: {what}")
    except Exception as e:  # server-side permission errors surface variously
        ok(f"DENIED as expected: {what} ({type(e).__name__})")
    else:
        raise AssertionError(f"NOT denied: {what}")


async def conn(user: str):
    errors: list[str] = []

    async def err_cb(e):
        errors.append(str(e))

    nc = await nats.connect(URL.format(u=user), error_cb=err_cb, max_reconnect_attempts=1)
    return nc, nc.jetstream(), errors


async def main() -> None:
    print("== usgs (zone-ingest) ==")
    nc, js, errs = await conn("usgs")
    await js.add_stream(StreamConfig(name="INGEST", subjects=["ingest.>"]))
    ok("usgs: STREAM.CREATE.INGEST")
    await js.publish("ingest.usgs", b'{"n":1}')
    ok("usgs: publish ingest.usgs (+ ack inbox)")
    await denied(js.stream_info("ENRICHED"), "usgs reading ENRICHED stream info")
    sub = await nc.subscribe("ingest.>")  # client-side OK; server must refuse delivery
    await js.publish("ingest.usgs", b'{"n":2}')
    try:
        await sub.next_msg(timeout=2)
        raise AssertionError("usgs received a message off ingest.> (subscribe not denied)")
    except NatsTimeout:
        assert any(
            "permissions violation" in e.lower() for e in errs
        ), f"no perm violation seen: {errs}"
        ok("usgs: subscribe ingest.> refused by broker")
    await nc.close()

    print("== noaa (zone-ingest, per-source-tight publish) ==")
    nc, js, _ = await conn("noaa")
    await js.publish("ingest.noaa", b'{"n":1}')
    ok("noaa: publish ingest.noaa (+ ack inbox)")
    await denied(js.publish("ingest.usgs", b'{"n":1}'), "noaa publishing ingest.usgs")
    await denied(js.stream_info("ENRICHED"), "noaa reading ENRICHED stream info")
    await nc.close()

    print("== gdacs (zone-ingest, per-source-tight publish) ==")
    nc, js, _ = await conn("gdacs")
    await js.publish("ingest.gdacs", b'{"n":1}')
    ok("gdacs: publish ingest.gdacs (+ ack inbox)")
    await denied(js.publish("ingest.noaa", b'{"n":1}'), "gdacs publishing ingest.noaa")
    await nc.close()

    print("== wikipedia (zone-ingest, per-source-tight publish) ==")
    nc, js, _ = await conn("wikipedia")
    await js.publish("ingest.wikipedia", b'{"n":1}')
    ok("wikipedia: publish ingest.wikipedia (+ ack inbox)")
    await denied(js.publish("ingest.noaa", b'{"n":1}'), "wikipedia publishing ingest.noaa")
    await nc.close()

    print("== bluesky (zone-ingest, per-source-tight publish) ==")
    nc, js, _ = await conn("bluesky")
    await js.publish("ingest.bluesky", b'{"n":1}')
    ok("bluesky: publish ingest.bluesky (+ ack inbox)")
    await denied(js.publish("ingest.noaa", b'{"n":1}'), "bluesky publishing ingest.noaa")
    await nc.close()

    print("== mastodon (zone-ingest, per-source-tight publish) ==")
    nc, js, _ = await conn("mastodon")
    await js.publish("ingest.mastodon", b'{"n":1}')
    ok("mastodon: publish ingest.mastodon (+ ack inbox)")
    await denied(js.publish("ingest.noaa", b'{"n":1}'), "mastodon publishing ingest.noaa")
    await nc.close()

    print("== gdelt (zone-ingest, per-source-tight publish) ==")
    nc, js, _ = await conn("gdelt")
    await js.publish("ingest.gdelt", b'{"n":1}')
    ok("gdelt: publish ingest.gdelt (+ ack inbox)")
    await denied(js.publish("ingest.noaa", b'{"n":1}'), "gdelt publishing ingest.noaa")
    await nc.close()

    print("== retrain (models-KV writer) ==")
    nc, js, _ = await conn("retrain")
    kv = await js.create_key_value(bucket="models")
    ok("retrain: create KV_models bucket")
    await kv.put("classifier", b"onnx-bytes")
    await kv.put("classifier.meta", b'{"version":"abc"}')
    entry = await kv.get("classifier")
    assert entry.value == b"onnx-bytes"
    ok("retrain: put + read-back models KV (eval baseline)")
    await denied(js.publish("enriched.rogue", b"{}"), "retrain publishing into enriched.>")
    await denied(js.stream_info("INGEST"), "retrain reading INGEST")
    await denied(js.stream_info("KV_dedup"), "retrain touching the dedup KV")
    await nc.close()

    print("== enrich (inference) ==")
    nc, js, _ = await conn("enrich")
    await js.add_stream(StreamConfig(name="INGEST", subjects=["ingest.>"]))
    await js.add_stream(StreamConfig(name="ENRICHED", subjects=["enriched.>"]))
    ok("enrich: ensure INGEST + ENRICHED")
    psub = await js.pull_subscribe("ingest.>", durable="enrich", stream="INGEST")
    msgs = await psub.fetch(2, timeout=5)
    for m in msgs:
        await m.ack()
    assert len(msgs) == 2
    ok("enrich: durable pull INGEST + fetch + ack")
    await js.publish("enriched.usgs", b'{"e":1}')
    ok("enrich: publish enriched.usgs")
    kv = await js.create_key_value(bucket="dedup")
    await kv.create("h1", b"1")
    try:
        await kv.create("h1", b"1")
        raise AssertionError("KV create-if-absent did not conflict")
    except KeyWrongLastSequenceError:
        ok("enrich: KV dedup create + conflict-on-existing")
    await js.add_stream(StreamConfig(name="TALLY", subjects=["tally.>"]))
    await js.publish("tally.minute", b'{"t":1}')
    ok("enrich: ensure TALLY + publish tally.minute")
    await denied(
        js.pull_subscribe("tally.>", durable="enrich-tally", stream="TALLY"),
        "enrich consuming its own TALLY stream",
    )
    await denied(js.stream_info("LLM_HEAVY"), "enrich touching LLM_HEAVY")
    # §6.3 online retrain: enrich READS the models KV retrain populated above (bind +
    # get), but must NOT be able to write it (that's retrain's job, gated by evalx).
    models = await js.key_value("models")
    entry = await models.get("classifier")
    assert entry.value == b"onnx-bytes"
    ok("enrich: bind + read models KV (hot-swap source)")
    await denied(js.publish("$KV.models.classifier", b"forged"), "enrich writing the models KV")
    await nc.close()

    print("== writer (db-writer) ==")
    nc, js, _ = await conn("writer")
    await js.add_stream(StreamConfig(name="ENRICHED", subjects=["enriched.>"]))
    psub = await js.pull_subscribe("enriched.>", durable="writer", stream="ENRICHED")
    msgs = await psub.fetch(1, timeout=5)
    await msgs[0].ack()
    ok("writer: durable pull ENRICHED + ack")
    await js.add_stream(StreamConfig(name="TALLY", subjects=["tally.>"]))
    psub = await js.pull_subscribe("tally.>", durable="writer-tally", stream="TALLY")
    msgs = await psub.fetch(1, timeout=5)
    await msgs[0].ack()
    ok("writer: durable pull TALLY + ack")
    await denied(js.publish("enriched.rogue", b"{}"), "writer publishing into enriched.>")
    await denied(js.publish("tally.rogue", b"{}"), "writer publishing into tally.>")
    await nc.close()

    print("== cluster ==")
    nc, js, _ = await conn("cluster")
    psub = await js.pull_subscribe("enriched.>", durable="cluster", stream="ENRICHED")
    msgs = await psub.fetch(1, timeout=5)
    await msgs[0].ack()
    ok("cluster: durable pull ENRICHED + ack")
    await js.add_stream(StreamConfig(name="LLM_HEAVY", subjects=["llm.>"]))
    await js.publish("llm.heavy", b'{"c":1}')
    ok("cluster: ensure LLM_HEAVY + publish llm.heavy")
    await denied(js.stream_info("KV_dedup"), "cluster touching the dedup KV")
    await nc.close()

    print("== claimx ==")
    nc, js, _ = await conn("claimx")
    await js.add_stream(StreamConfig(name="LLM_HEAVY", subjects=["llm.>"]))
    await js.add_stream(StreamConfig(name="CLAIM", subjects=["claim.>"]))
    psub = await js.pull_subscribe("llm.heavy", durable="claimx", stream="LLM_HEAVY")
    msgs = await psub.fetch(1, timeout=5)
    await msgs[0].ack()
    ok("claimx: durable pull llm.heavy + ack")
    await js.publish("claim.extracted", b'{"claim":"x"}')
    ok("claimx: publish claim.extracted")
    await denied(js.stream_info("INGEST"), "claimx reading INGEST")
    await nc.close()

    print("== trust (claim consumer, publish-nothing) ==")
    nc, js, _ = await conn("trust")
    await js.add_stream(StreamConfig(name="CLAIM", subjects=["claim.>"]))
    psub = await js.pull_subscribe("claim.>", durable="trust", stream="CLAIM")
    msgs = await psub.fetch(1, timeout=5)
    await msgs[0].ack()
    ok("trust: durable pull CLAIM + ack")
    await denied(js.publish("claim.forged", b"{}"), "trust publishing into claim.>")
    await denied(js.stream_info("ENRICHED"), "trust reading ENRICHED")
    await nc.close()

    print(f"\nALL {len(OK)} PERMISSION CHECKS PASSED")


asyncio.run(main())
