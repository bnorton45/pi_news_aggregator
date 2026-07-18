"""Local embedder (PLAN §6.3 step 3).

Real path: a quantized MiniLM/BGE-small ONNX model on CPU (the Pi target).
Dev fallback: a deterministic hash embedder so the data path runs end-to-end
*without* shipping a model — the fallback only needs a vector of the right shape.

Swap is a single env var (EMBED_MODEL_PATH); nothing downstream changes.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Protocol

import numpy as np

from libs.schema import EMBED_DIM  # single source of truth (must match vector(384))

log = logging.getLogger("embed")


class Embedder(Protocol):
    dim: int

    def encode(self, texts: list[str]) -> np.ndarray: ...


class HashEmbedder:
    """Deterministic, dependency-light stand-in. NOT semantic — dev/CI only."""

    dim = EMBED_DIM

    def encode(self, texts: list[str]) -> np.ndarray:
        out = np.empty((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            # Expand a digest into dim floats, then L2-normalize (cosine-ready).
            buf = bytearray()
            n = 0
            while len(buf) < self.dim * 4:
                buf += hashlib.sha256(f"{n}:{t}".encode()).digest()
                n += 1
            vec = np.frombuffer(bytes(buf[: self.dim * 4]), dtype=np.uint32).astype(np.float32)
            vec = vec / np.iinfo(np.uint32).max - 0.5
            norm = np.linalg.norm(vec) or 1.0
            out[i] = vec / norm
        return out


def load_embedder() -> Embedder:
    """ONNX model if EMBED_MODEL_PATH is set & loadable, else the dev fallback."""
    path = os.environ.get("EMBED_MODEL_PATH")
    if path:
        try:
            from libs.embed.onnx_embedder import OnnxEmbedder  # lazy: heavy deps

            log.info("loading ONNX embedder from %s", path)
            return OnnxEmbedder(path)
        except Exception:
            log.exception("ONNX embedder failed to load; falling back to HashEmbedder")
    log.warning("using HashEmbedder (dev fallback — non-semantic vectors)")
    return HashEmbedder()
