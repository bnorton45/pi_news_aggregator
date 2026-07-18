"""Retrain loop tests (PLAN §6.3 step 2, §4): train→ONNX→score round-trip, the
time-ordered eval split, and the promotion gate that contains weak-label poisoning."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from libs.classify.onnx_classifier import OnnxClassifier
from services.retrain.evalx import (
    LabeledRow,
    class_counts,
    evaluate,
    f1_metrics,
    meets_floors,
    predict_proba,
    should_promote,
    time_split,
)
from services.retrain.train import train_model

# ── separable synthetic corpus: an "earthquake" cue = positive, "lunch" = negative ──
_POS = [f"breaking major earthquake strikes the coast overnight sample {i}" for i in range(30)]
_NEG = [f"had a lovely quiet lunch with friends today sample {i}" for i in range(30)]


def _corpus() -> tuple[list[str], list[int]]:
    return _POS + _NEG, [1] * len(_POS) + [0] * len(_NEG)


def _rows(base: datetime | None = None) -> list[LabeledRow]:
    base = base or datetime(2026, 7, 6, tzinfo=UTC)
    texts, labels = _corpus()
    # interleave so a time split still sees both classes in each side
    return [
        LabeledRow(base + timedelta(minutes=i), t, y)
        for i, (t, y) in enumerate(zip(texts, labels, strict=True))
    ]


# ── train → ONNX → OnnxClassifier ───────────────────────────────────────────────
def test_train_round_trip_separates_classes() -> None:
    texts, labels = _corpus()
    clf = OnnxClassifier(train_model(texts, labels, epochs=400))
    assert clf.score("breaking earthquake destroys the bridge") > 0.5
    assert clf.score("relaxed lovely lunch with friends") < 0.5


def test_onnx_from_bytes_and_path_agree(tmp_path) -> None:
    texts, labels = _corpus()
    blob = train_model(texts, labels, epochs=200)
    from_bytes = OnnxClassifier(blob)
    p = tmp_path / "m.onnx"
    p.write_bytes(blob)
    from_path = OnnxClassifier(str(p))
    probe = "major earthquake breaking news"
    assert abs(from_bytes.score(probe) - from_path.score(probe)) < 1e-6


def test_onnx_garbage_bytes_raise() -> None:
    # The enrich hot-swap relies on this: a bad artifact must fail loudly so the
    # watcher falls back to the current model instead of serving noise.
    raised = False
    try:
        OnnxClassifier(b"definitely not an onnx model")
    except Exception:
        raised = True
    assert raised, "garbage bytes must not load as a usable model"


def test_predict_proba_batches() -> None:
    texts, labels = _corpus()
    blob = train_model(texts, labels, epochs=200)
    probs = predict_proba(blob, ["earthquake breaking", "quiet lunch today"])
    assert len(probs) == 2
    assert all(0.0 <= p <= 1.0 for p in probs)
    assert probs[0] > probs[1]


# ── eval harness: time split + metrics ──────────────────────────────────────────
def test_time_split_is_ordered_and_disjoint() -> None:
    rows = _rows()
    train, ev = time_split(rows, eval_frac=0.25)
    assert len(ev) == round(len(rows) * 0.25)
    assert len(train) + len(ev) == len(rows)
    # every eval row is strictly newer than every train row (no leakage)
    assert max(r.ts_observed for r in train) <= min(r.ts_observed for r in ev)


def test_time_split_tiny_inputs() -> None:
    assert time_split([]) == ([], [])
    one = [LabeledRow(datetime(2026, 1, 1, tzinfo=UTC), "x", 1)]
    assert time_split(one) == (one, [])  # never eval on the only row
    two = _rows()[:2]
    tr, ev = time_split(two)
    assert len(tr) == 1 and len(ev) == 1


def test_f1_metrics_perfect_and_zero() -> None:
    perfect = f1_metrics([0.9, 0.1, 0.8], [1, 0, 1])
    assert perfect["f1"] == 1.0
    # all-wrong predictions → zero precision/recall
    wrong = f1_metrics([0.9, 0.9], [0, 0])
    assert wrong["f1"] == 0.0


def test_evaluate_on_trained_model_is_strong() -> None:
    texts, labels = _corpus()
    blob = train_model(texts, labels, epochs=400)
    metrics = evaluate(blob, _rows())
    assert metrics["f1"] > 0.8  # learns the separable cue


# ── promotion gate: floors + non-regression ─────────────────────────────────────
def test_class_counts() -> None:
    c = class_counts(_rows())
    assert c[1] == len(_POS) and c[0] == len(_NEG)


def test_floors_reject_too_few_rows() -> None:
    ok, reason = meets_floors(199, Counter({0: 100, 1: 99}))
    assert not ok and "too few" in reason


def test_floors_reject_underpopulated_class() -> None:
    ok, reason = meets_floors(400, Counter({0: 5, 1: 395}))
    assert not ok and "class 0" in reason


def test_gate_promotes_first_model_when_floors_met() -> None:
    ok, _ = should_promote(0.6, None, 400, Counter({0: 200, 1: 200}))
    assert ok


def test_gate_refuses_regression() -> None:
    ok, reason = should_promote(0.5, 0.9, 400, Counter({0: 200, 1: 200}))
    assert not ok and "candidate F1" in reason


def test_gate_promotes_on_tie() -> None:
    ok, _ = should_promote(0.9, 0.9, 400, Counter({0: 200, 1: 200}))
    assert ok


def test_gate_floors_beat_a_better_f1() -> None:
    # A great candidate on too little data is still refused (poisoning defense).
    ok, reason = should_promote(1.0, None, 10, Counter({0: 5, 1: 5}))
    assert not ok and "too few" in reason
