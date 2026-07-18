"""Cluster layer (PLAN §6.4, §6.5).

- ``main`` — online clustering: partition-pruned pgvector ANN assigns each enriched Item
  to a Story or opens a new one; gates ``llm.heavy`` for candidate Stories (§6.3).
- ``claimx`` — the LLM side: runs the Ollama small model (qwen3:1.7b) on the gated queue
  (no DB, §3.3); the 4B reasons-into-content on this extractive task.
- ``cluster`` — pure decision logic; ``db`` — ANN + Story persistence.
"""
