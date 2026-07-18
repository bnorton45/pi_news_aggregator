"""64-bit simhash for near-duplicate text detection (PLAN §6.3 step 2, §6.5 edge b).

Charikar simhash over word-level shingles: each feature's 64-bit hash votes per
bit position; the sign of each tally becomes that bit. Near-duplicate texts
(copypasta with small edits) land within a few bits of Hamming distance.

Used two ways:
- Provenance edges (§6.5 b): pairwise WITHIN a Story's members — bounded by story
  size, so no index needed there.
- Firehose near-dedup (§6.3 step 2): the future LSH banding layer will band these
  same fingerprints; keep this module dependency-free so both paths share it.

The value is returned unsigned (0..2^64-1). Postgres `bigint` is signed — use
`to_signed64`/`from_signed64` at the storage boundary.
"""

from __future__ import annotations

import hashlib
import re

_TOKEN = re.compile(r"\w+", re.UNICODE)
_SHINGLE = 2  # word bigrams: robust to reordering noise, cheap to compute


def _features(text: str) -> list[str]:
    toks = _TOKEN.findall(text.casefold())
    if len(toks) < _SHINGLE:
        return [" ".join(toks)] if toks else []
    return [" ".join(toks[i : i + _SHINGLE]) for i in range(len(toks) - _SHINGLE + 1)]


def _h64(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big")


def simhash64(text: str) -> int:
    """Simhash fingerprint of `text`; 0 for empty/whitespace-only input."""
    feats = _features(text)
    if not feats:
        return 0
    tally = [0] * 64
    for f in feats:
        h = _h64(f)
        for bit in range(64):
            tally[bit] += 1 if (h >> bit) & 1 else -1
    out = 0
    for bit in range(64):
        if tally[bit] > 0:
            out |= 1 << bit
    return out


def hamming64(a: int, b: int) -> int:
    return ((a ^ b) & 0xFFFFFFFFFFFFFFFF).bit_count()


def to_signed64(v: int) -> int:
    """Map unsigned 64-bit → signed (Postgres bigint)."""
    return v - (1 << 64) if v >= (1 << 63) else v


def from_signed64(v: int) -> int:
    """Inverse of to_signed64."""
    return v + (1 << 64) if v < 0 else v
