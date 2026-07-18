"""ONNX relevance classifier (PLAN §6.3 step 2) — the real path behind load_classifier().

Runs the hashing-logistic-regression graph (MatMul + Add + Sigmoid) authored by
services/retrain over the feature-hashed BoW of the item text. Constructed from a path
OR raw bytes: the retrain loop hot-swaps by handing enrich the model bytes straight off
the NATS KV, so an image with a read-only rootfs (PLAN §3.2) never has to spill the
artifact to disk to load it.
"""

from __future__ import annotations

from libs.classify.featurize import featurize


class OnnxClassifier:
    """Relevance in [0,1] from an ONNX logistic-regression model over hashed features."""

    def __init__(self, path_or_bytes: str | bytes) -> None:
        import onnxruntime as ort  # heavy dep — lazy, as load_classifier() assumes

        model = path_or_bytes
        if isinstance(model, str):
            with open(model, "rb") as fh:
                model = fh.read()
        # onnxruntime raises on a malformed graph here → callers (load_classifier, the
        # enrich hot-swap) catch and fall back rather than serve a broken model.
        self._sess = ort.InferenceSession(model, providers=["CPUExecutionProvider"])
        self._input = self._sess.get_inputs()[0].name

    def score(self, text: str) -> float:
        (out,) = self._sess.run(None, {self._input: featurize(text)})
        return float(out.reshape(-1)[0])
