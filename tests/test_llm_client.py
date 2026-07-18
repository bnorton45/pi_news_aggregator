"""Local Ollama client tests (libs/llm) — payload shape, no live server.

The §3.3 guardrail that matters most here: the enrichment LLM gets NO tool surface, so the
request MUST NOT carry a `tools` key. We drive /api/chat through an httpx MockTransport and
assert the wire payload rather than reaching a real Ollama.
"""

from __future__ import annotations

import json

import httpx
import pytest

from libs.llm.client import OllamaClient, OllamaConfig


def _client_capturing(captured: dict, reply: str = "a claim.") -> OllamaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": reply}})

    c = OllamaClient(OllamaConfig(base_url="http://ollama.test"))
    # swap the internal client for a mocked-transport one (same base_url)
    c._client = httpx.AsyncClient(
        base_url="http://ollama.test", transport=httpx.MockTransport(handler)
    )
    return c


async def test_chat_has_no_tool_surface_and_default_flags() -> None:
    captured: dict = {}
    c = _client_capturing(captured, reply="  the dam failed near Kyiv.  ")
    out = await c.chat(system="sys", prompt="hi")
    await c.aclose()

    assert out == "  the dam failed near Kyiv.  "  # returned verbatim; caller trims/validates
    assert "tools" not in captured  # §3.3: enrichment LLMs get no tool surface
    assert captured["stream"] is False
    assert captured["think"] is False  # non-thinking by default (§6.3 mode policy)
    # keep_alive must be a NUMBER (-1 = resident forever); Ollama rejects the bare "-1" str
    assert captured["keep_alive"] == -1
    assert captured["model"] == "qwen3:1.7b"  # claim path uses the small non-reasoner
    assert captured["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


async def test_chat_think_and_format_passthrough() -> None:
    captured: dict = {}
    c = _client_capturing(captured)
    await c.chat(prompt="p", think=True, fmt="json")
    await c.aclose()

    assert captured["think"] is True
    assert captured["format"] == "json"
    # no system message when none is given
    assert captured["messages"] == [{"role": "user", "content": "p"}]


async def test_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_URL", "http://ollama:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:1.7b")
    monkeypatch.delenv("OLLAMA_KEEP_ALIVE", raising=False)
    cfg = OllamaConfig.from_env()
    assert cfg.base_url == "http://ollama:11434"
    assert cfg.model == "qwen3:1.7b"
    assert cfg.keep_alive == -1  # default: number, not the invalid "-1" string


def test_keep_alive_from_env_coercion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "300")
    assert OllamaConfig.from_env().keep_alive == 300  # numeric string -> int seconds
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "5m")
    assert OllamaConfig.from_env().keep_alive == "5m"  # unit'd duration stays a string
