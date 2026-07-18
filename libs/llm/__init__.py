"""Local Ollama client for tiered enrichment (PLAN §6.3, §3.3 pure-function guardrails)."""

from libs.llm.client import OllamaClient, OllamaConfig

__all__ = ["OllamaClient", "OllamaConfig"]
