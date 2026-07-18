"""Pure-python BERT WordPiece tokenizer (PLAN §6.3).

Shared by the ONNX embedder (bge-small) and the ONNX NER token-classifier. Deliberately
dependency-free: no HuggingFace `tokenizers` Rust wheel, so the hardened enrich image
stays minimal (PLAN §3.2) and there is no aarch64-wheel to break the arm64-smoke gate on
the Pi target. Survivors-only throughput (≤5/s post-admission) makes pure-python ample.
"""

from __future__ import annotations

from libs.tokenize.wordpiece import Encoding, WordPieceTokenizer

__all__ = ["Encoding", "WordPieceTokenizer"]
