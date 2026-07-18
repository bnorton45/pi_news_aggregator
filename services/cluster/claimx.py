"""Claim-extraction worker (PLAN §6.3 tiering, §3.3): the LLM side of clustering.

Consumes ClaimRequest off `llm.heavy` (gated to candidate Stories by the cluster
service — the claim model never sees the full survivor stream), runs the local Ollama
small model (qwen3:1.7b) to isolate the factual assertion, and publishes a ClaimResult
on `claim.*`. Claim extraction uses the 1.7b, NOT the 4B: the 4B reasons INTO content on
this extractive task and emits garbage (docs/pi-throughput-findings.md).

Pure-function / no DB (PLAN §3.3): this process *parses untrusted text through a model*
— the RCE surface — so it holds no database credentials. The model output is
schema-validated into ClaimResult before publish.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import replace

from libs.bus import BusConfig, ScopedPublisher, connect, consume_validated, ensure_stream
from libs.llm import OllamaClient, OllamaConfig
from libs.schema import ClaimRequest, ClaimResult

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("claimx")

SUBJECT = os.environ.get("CLAIMX_SUBJECT", "llm.heavy")
DURABLE = os.environ.get("CLAIMX_DURABLE", "claimx")
CLAIM_PREFIX = "claim."

_SYSTEM = (
    "You extract the single factual claim asserted by a social post, as one terse "
    "sentence. If there is no checkable factual claim, reply with an empty line. "
    "Output only the claim, no preamble."
)


class ClaimExtractor:
    def __init__(self, llm: OllamaClient, publisher: ScopedPublisher) -> None:
        self.llm = llm
        self.pub = publisher
        self.extracted = 0

    async def handle(self, req: ClaimRequest) -> None:
        raw = await self.llm.chat(system=_SYSTEM, prompt=req.text)  # non-thinking by default
        result = ClaimResult(story_id=req.story_id, item_id=req.item_id, claim=raw.strip()[:2048])
        await self.pub.publish(f"{CLAIM_PREFIX}extracted", result)
        self.extracted += 1
        if self.extracted % 50 == 0:
            log.info("extracted=%d", self.extracted)


async def run() -> None:
    cfg = replace(
        BusConfig.from_env(),
        stream=os.environ.get("LLM_STREAM", "LLM_HEAVY"),
        subjects=("llm.>",),
    )
    nc, js = await connect(cfg)
    await ensure_stream(js, cfg)  # LLM_HEAVY (consume)
    claim_cfg = replace(cfg, stream=os.environ.get("CLAIM_STREAM", "CLAIM"), subjects=("claim.>",))
    await ensure_stream(js, claim_cfg)  # CLAIM (publish)
    llm = OllamaClient(OllamaConfig.from_env())  # honor OLLAMA_URL / OLLAMA_MODEL (in-cluster svc)
    extractor = ClaimExtractor(llm, ScopedPublisher(js, CLAIM_PREFIX, cfg.max_msg_bytes))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info(
        "claimx up: in=%s out=%sextracted durable=%s model=%s",
        SUBJECT,
        CLAIM_PREFIX,
        DURABLE,
        llm.cfg.model,
    )
    consumer = asyncio.create_task(
        consume_validated(
            js,
            subject=SUBJECT,
            durable=DURABLE,
            stream=cfg.stream,
            model=ClaimRequest,
            handler=extractor.handle,
            max_msg_bytes=cfg.max_msg_bytes,
        )
    )
    await stop.wait()
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)
    await llm.aclose()
    await nc.drain()
    log.info("shutdown: extracted=%d", extractor.extracted)


if __name__ == "__main__":
    asyncio.run(run())
