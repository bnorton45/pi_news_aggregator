"""Trainer for the relevance classifier (PLAN §6.3 step 2, §4).

numpy logistic regression over feature-hashed weak-label rows, exported to the ONNX
graph OnnxClassifier serves (MatMul + Add + Sigmoid). Deliberately the cheap
hashing-LR family: the deliverable is the *machinery* (weak labels → train →
eval gate → KV publish → hot-swap), not the model — a future MiniLM/SetFit head swaps
in under the same artifact contract. No sklearn: the graph is authored by
hand so the only runtime dep is onnxruntime.
"""

from __future__ import annotations

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

from libs.classify.featurize import DIM, featurize

# ONNX target kept conservative so the artifact loads on the enrich image's pinned
# onnxruntime (>=1.18): IR 9 + opset 13, both far below anything 1.18 rejects.
_IR_VERSION = 9
_OPSET = 13


def featurize_batch(texts: list[str]) -> np.ndarray:
    """Texts → ``float32[N, DIM]`` (each row L2-normalized, same as serve time)."""
    if not texts:
        return np.zeros((0, DIM), dtype=np.float32)
    return np.vstack([featurize(t) for t in texts]).astype(np.float32)


def train_lr(
    x: np.ndarray,
    y: np.ndarray,
    *,
    epochs: int = 300,
    lr: float = 0.5,
    l2: float = 1e-4,
) -> tuple[np.ndarray, float]:
    """Full-batch gradient-descent logistic regression. Returns (weights[DIM], bias)."""
    n, d = x.shape
    w = np.zeros(d, dtype=np.float64)
    b = 0.0
    yf = y.astype(np.float64)
    for _ in range(epochs):
        p = 1.0 / (1.0 + np.exp(-(x @ w + b)))
        g = p - yf  # dL/dz, shape [n]
        w -= lr * (x.T @ g / n + l2 * w)
        b -= lr * float(g.mean())
    return w, b


def to_onnx(w: np.ndarray, b: float) -> bytes:
    """Author the logistic-regression graph as ONNX bytes: sigmoid(features·W + B).

    Input `features` is ``float32[N, DIM]`` (dynamic batch so evalx can score a whole
    slice in one run and OnnxClassifier a single row); output `relevance` is [N, 1]."""
    weight = numpy_helper.from_array(w.astype(np.float32).reshape(DIM, 1), name="W")
    bias = numpy_helper.from_array(np.array([b], dtype=np.float32), name="B")
    x = helper.make_tensor_value_info("features", TensorProto.FLOAT, [None, DIM])
    y = helper.make_tensor_value_info("relevance", TensorProto.FLOAT, [None, 1])
    nodes = [
        helper.make_node("MatMul", ["features", "W"], ["logits_raw"]),
        helper.make_node("Add", ["logits_raw", "B"], ["logits"]),
        helper.make_node("Sigmoid", ["logits"], ["relevance"]),
    ]
    graph = helper.make_graph(nodes, "hashing-lr", [x], [y], [weight, bias])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", _OPSET)])
    model.ir_version = _IR_VERSION
    onnx.checker.check_model(model)
    return model.SerializeToString()


def train_model(texts: list[str], labels: list[int], **kw: float) -> bytes:
    """Convenience: featurize + train + export to ONNX bytes in one call."""
    x = featurize_batch(texts)
    y = np.asarray(labels, dtype=np.float64)
    w, b = train_lr(x, y, **kw)  # type: ignore[arg-type]
    return to_onnx(w, b)
