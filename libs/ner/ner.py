"""Per-survivor NER + geo (PLAN §6.3 step 4): entity extraction on admitted survivors.

Runs AFTER the admission gate, on survivors only (≤400k/day) — never the full firehose
(that's the cheap gazetteer tally, §6.3 step 2). Real path: a small ONNX token-classifier
(swap via NER_MODEL_PATH). Dev/0a fallback: a no-op — coarse entities are already populated
by the gazetteer tally (libs/gazetteer), so storage still has entities; the model refines
them when provided. Claim extraction is NOT here — it is Story-level (PLAN §6.4).
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

from libs.schema import Entity

log = logging.getLogger("ner")


class Ner(Protocol):
    def extract(self, text: str) -> list[Entity]:
        """Entities (person/org/place + geo) found in `text`."""
        ...


class NoOpNer:
    """Dev/0a fallback: entities come from the gazetteer tally; the model refines later."""

    def extract(self, text: str) -> list[Entity]:
        return []


def load_ner() -> Ner:
    """ONNX NER if NER_MODEL_PATH is set & loadable, else the no-op fallback."""
    path = os.environ.get("NER_MODEL_PATH")
    if path:
        try:
            from libs.ner.onnx_ner import OnnxNer  # lazy: heavy deps

            log.info("loading ONNX NER from %s", path)
            return OnnxNer(path)
        except Exception:
            log.exception("ONNX NER failed to load; falling back to no-op")
    log.info("using NoOpNer (dev fallback — entities from gazetteer tally only)")
    return NoOpNer()
