"""Feature hashing for the retrain classifier (PLAN §6.3 step 2).

Deterministic bag-of-words → fixed ``float32[1, 2^14]`` via the hashing trick, so the
trainer (services/retrain) and the inference-time OnnxClassifier featurize IDENTICALLY
with no shared vocabulary file — the 5-day wall (§4) forbids a growing, persisted vocab.

`hashlib`, never the builtin `hash()`: the latter is per-process salted (PYTHONHASHSEED),
which would make train-time and serve-time features silently disagree. The signed hashing
trick (an independent bit picks each token's sign) unbiases bucket collisions — colliding
tokens cancel in expectation instead of always adding.
"""

from __future__ import annotations

import hashlib
import re

import numpy as np

DIM = 1 << 14  # 16384 hashed feature buckets — fixed; the ONNX graph's input width

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _bucket(token: str) -> tuple[int, float]:
    """Hash a token to (bucket index, ±1 sign) — both drawn from one blake2b digest."""
    n = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
    idx = n % DIM  # low bits pick the bucket
    sign = 1.0 if (n >> 20) & 1 else -1.0  # a higher, independent bit picks the sign
    return idx, sign


def featurize(text: str) -> np.ndarray:
    """Text → ``float32[1, DIM]`` feature-hashed BoW, L2-normalized (length-invariant so a
    fixed decision threshold holds regardless of item length). Empty text → all-zeros."""
    vec = np.zeros(DIM, dtype=np.float32)
    for tok in _tokens(text):
        idx, sign = _bucket(tok)
        vec[idx] += sign
    norm = float(np.linalg.norm(vec))
    if norm > 0.0:
        vec /= norm
    return vec.reshape(1, DIM)
