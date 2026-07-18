"""Relevance classifier (PLAN §6.3 step 2): "is this a newsworthy factual claim?" in [0,1].

**Text-only by design** (PLAN §6.3): it does NOT read entities/geo — those come from the
cheap gazetteer (velocity) and survivor NER (storage); the §6.3a gate composes them
separately. Real path: a fine-tuned sub-100MB encoder (MiniLM/DistilBERT) or SetFit head
exported to ONNX, run on the full firehose on ARM CPU; swap via CLASSIFY_MODEL_PATH. Dev/0a
fallback: a cheap text-cue heuristic so the data path runs without shipping a model (mirrors
libs/embed).

Filter warming (PLAN §6.3 step 2, §4): the real model ships seeded from a curated set and is
retrained online over the rolling 5-day window — the wall forbids a growing corpus.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

log = logging.getLogger("classify")

# Cheap newsworthiness cues for the 0a heuristic — NOT the real model (PLAN §6.3).
_NEWS_CUES = frozenset(
    {
        "breaking",
        "urgent",
        "explosion",
        "earthquake",
        "quake",
        "attack",
        "killed",
        "dead",
        "evacuat",
        "outbreak",
        "strike",
        "airstrike",
        "fire",
        "collapse",
        "shooting",
        "flood",
        "storm",
        "protest",
        "sanction",
        "missile",
        "wildfire",
        "hostage",
        "derail",
        "blast",
    }
)


class Classifier(Protocol):
    def score(self, text: str) -> float:
        """Relevance in [0,1]; higher = more likely a newsworthy factual claim."""
        ...


class HeuristicClassifier:
    """Text-only cue heuristic — dev/0a stand-in for the ONNX classifier (PLAN §6.3)."""

    def score(self, text: str) -> float:
        t = text.lower()
        score = 0.3
        if len(text) > 80:
            score += 0.2
        if any(cue in t for cue in _NEWS_CUES):
            score += 0.3
        return min(1.0, score)


def load_classifier() -> Classifier:
    """ONNX classifier if CLASSIFY_MODEL_PATH is set & loadable, else the heuristic."""
    path = os.environ.get("CLASSIFY_MODEL_PATH")
    if path:
        try:
            from libs.classify.onnx_classifier import OnnxClassifier  # lazy: heavy deps

            log.info("loading ONNX classifier from %s", path)
            return OnnxClassifier(path)
        except Exception:
            log.exception("ONNX classifier failed to load; falling back to heuristic")
    log.warning("using HeuristicClassifier (dev fallback — text cues, not a model)")
    return HeuristicClassifier()
