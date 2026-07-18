"""Local LLM client — Ollama / Qwen3-4B (PLAN §6.3, §3.3).

Pure-function guardrails (PLAN §3.3): the model classifies / extracts / summarizes
ONLY. This client exposes no tools, performs no DB writes, and talks solely to the
in-cluster Ollama service — there is no internet egress. The returned text is
UNTRUSTED: callers MUST schema-validate it (e.g. a Pydantic model) before use,
exactly like any other firehose-derived input.

Defaults follow the §6.3 mode policy: **non-thinking** (fast) unless `think=True`
(reserved for the on-demand analyst brief), and `keep_alive=-1` so the low-duty-cycle
4B stays resident instead of paying a cold reload per burst (PLAN §2).

Heavy enrichment is invoked via the bounded `llm.heavy` queue (PLAN §10).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

# In-cluster this is the Ollama Service, e.g. http://ollama.zone-process.svc:11434.
DEFAULT_URL = "http://localhost:11434"


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str = DEFAULT_URL
    # Claim extraction (the only caller) runs the small non-reasoner — the 4B reasons INTO
    # content on this extractive task (docs/pi-throughput-findings.md). Deployments
    # set OLLAMA_MODEL explicitly; this default just keeps it off the 4B.
    model: str = "qwen3:1.7b"
    timeout_s: float = 120.0  # a small model on Pi CPU is still slow; Story-level work tolerates it
    # Keep the model resident between bursts (PLAN §2). Ollama wants a NUMBER (-1 = forever,
    # 0 = unload now, seconds otherwise) or a unit'd duration string ("5m") — the bare
    # string "-1" is rejected ("time: missing unit"), so the default is the integer -1.
    keep_alive: int | str = -1

    @classmethod
    def from_env(cls) -> OllamaConfig:
        ka = os.environ.get("OLLAMA_KEEP_ALIVE")
        if ka is None:
            keep_alive: int | str = -1
        else:
            try:  # "-1"/"300" -> int seconds; "5m" stays a duration string
                keep_alive = int(ka)
            except ValueError:
                keep_alive = ka
        return cls(
            base_url=os.environ.get("OLLAMA_URL", DEFAULT_URL),
            model=os.environ.get("OLLAMA_MODEL", "qwen3:1.7b"),
            timeout_s=float(os.environ.get("OLLAMA_TIMEOUT_S", "120")),
            keep_alive=keep_alive,
        )


class OllamaClient:
    """Async, pure-function wrapper over Ollama's /api/chat (PLAN §3.3)."""

    def __init__(self, cfg: OllamaConfig | None = None) -> None:
        self.cfg = cfg or OllamaConfig()
        self._client = httpx.AsyncClient(base_url=self.cfg.base_url, timeout=self.cfg.timeout_s)

    async def chat(
        self,
        *,
        prompt: str,
        system: str | None = None,
        think: bool = False,
        fmt: dict[str, Any] | str | None = None,
    ) -> str:
        """One-shot completion. Returns raw text — the caller MUST validate it.

        `think=False` (default) runs non-thinking for fast bulk work; set True only
        for the latency-tolerant analyst brief. `fmt` may be "json" or a JSON schema
        for structured output (Ollama `format`).
        """
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": False,
            "think": think,
            "keep_alive": self.cfg.keep_alive,
            # No `tools` key: enrichment LLMs get no tool surface (PLAN §3.3).
        }
        if fmt is not None:
            payload["format"] = fmt

        resp = await self._client.post("/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("message", {}).get("content", "")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OllamaClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
