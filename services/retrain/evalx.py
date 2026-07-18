"""Eval harness + promotion gate (PLAN §6.3 step 2, §4) — shared machinery.

Time-ordered split (train on the older rows, eval on the newest ~20%) so a model is
never scored on rows it trained on. The promotion gate is the one defense against a
weak-label poisoning attack landing in production: a candidate ships ONLY if it beats
the incumbent on the held-out slice AND clears the data floors.

Importable without onnxruntime/asyncpg (the pure split/metric/gate functions are what
main.py and the tests exercise); `python -m services.retrain.evalx` doubles as a
standalone CLI that scores the live KV model against the current window.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

MIN_ROWS = int(os.environ.get("RETRAIN_MIN_ROWS", "200"))
MIN_PER_CLASS = int(os.environ.get("RETRAIN_MIN_PER_CLASS", "30"))
EVAL_FRAC = float(os.environ.get("RETRAIN_EVAL_FRAC", "0.2"))
THRESHOLD = float(os.environ.get("RETRAIN_THRESHOLD", "0.5"))


@dataclass(frozen=True)
class LabeledRow:
    ts_observed: datetime
    text: str
    label: int


def class_counts(rows: list[LabeledRow]) -> Counter[int]:
    return Counter(r.label for r in rows)


def time_split(
    rows: list[LabeledRow], eval_frac: float = EVAL_FRAC
) -> tuple[list[LabeledRow], list[LabeledRow]]:
    """Oldest → train, newest `eval_frac` → eval (no leakage across the time boundary).
    Guarantees ≥1 eval and ≥1 train row when there are ≥2 rows."""
    ordered = sorted(rows, key=lambda r: r.ts_observed)
    n = len(ordered)
    n_eval = min(n - 1, max(1, round(n * eval_frac))) if n >= 2 else 0
    cut = n - n_eval
    return ordered[:cut], ordered[cut:]


def f1_metrics(scores: list[float], labels: list[int], threshold: float = THRESHOLD) -> dict:
    """Precision/recall/F1 of the positive class at `threshold`."""
    tp = fp = fn = 0
    for s, y in zip(scores, labels, strict=True):
        pred = 1 if s >= threshold else 0
        if pred == 1 and y == 1:
            tp += 1
        elif pred == 1 and y == 0:
            fp += 1
        elif pred == 0 and y == 1:
            fn += 1
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": precision, "recall": recall, "f1": f1, "n": len(labels)}


def predict_proba(model_bytes: bytes, texts: list[str]) -> list[float]:
    """Batch-score `texts` through an ONNX model's sigmoid output (onnxruntime)."""
    import onnxruntime as ort

    from services.retrain.train import featurize_batch

    if not texts:
        return []
    sess = ort.InferenceSession(model_bytes, providers=["CPUExecutionProvider"])
    name = sess.get_inputs()[0].name
    (out,) = sess.run(None, {name: featurize_batch(texts)})
    return [float(v) for v in out.reshape(-1)]


def evaluate(model_bytes: bytes, rows: list[LabeledRow]) -> dict:
    scores = predict_proba(model_bytes, [r.text for r in rows])
    return f1_metrics(scores, [r.label for r in rows])


def meets_floors(n_labeled: int, counts: Counter[int]) -> tuple[bool, str]:
    """Data-sufficiency floors, checked before spending a training cycle."""
    if n_labeled < MIN_ROWS:
        return False, f"too few labeled rows ({n_labeled} < {MIN_ROWS})"
    for cls in (0, 1):
        if counts.get(cls, 0) < MIN_PER_CLASS:
            return False, f"class {cls} underpopulated ({counts.get(cls, 0)} < {MIN_PER_CLASS})"
    return True, "floors met"


def should_promote(
    candidate_f1: float,
    current_f1: float | None,
    n_labeled: int,
    counts: Counter[int],
) -> tuple[bool, str]:
    """The gate: floors first, then candidate must not regress F1 on the eval slice.
    `current_f1 is None` means no incumbent (first cycle) — floors alone decide."""
    ok, reason = meets_floors(n_labeled, counts)
    if not ok:
        return False, reason
    if current_f1 is not None and candidate_f1 < current_f1:
        return False, f"candidate F1 {candidate_f1:.3f} < current {current_f1:.3f}"
    baseline = current_f1 if current_f1 is not None else 0.0
    return True, f"promote (F1 {candidate_f1:.3f} ≥ {baseline:.3f})"


async def _cli() -> None:
    """Score the live KV model against the current labeled window and print metrics."""
    import json

    from libs.classify.model_store import ModelStore
    from services.retrain.db import RetrainDb

    bucket = os.environ.get("MODELS_KV_BUCKET", "models")
    db = RetrainDb()
    await db.connect()
    rows = await db.fetch_labels(int(os.environ.get("RETRAIN_MAX_ROWS", "5000")))
    _, ev = time_split(rows)
    store = await ModelStore.connect(bucket)
    model = await store.get_model()
    if model is None:
        print(json.dumps({"error": "no model in KV", "labeled_rows": len(rows)}))
    else:
        print(json.dumps({"eval": evaluate(model, ev), "meta": await store.get_meta()}))
    await db.close()
    await store.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(_cli())
