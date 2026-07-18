"""ONNX sentence embedder (PLAN §6.3 step 3) — the real path behind load_embedder().

bge-small-en-v1.5 on CPU: WordPiece-tokenize → transformer → **CLS-token pooling + L2
normalize** (the bge convention — NOT mean pooling). Output is float32 (n, 384),
L2-normalized so pgvector's cosine `<=>` is a plain dot product downstream.

`path` is a directory holding `model.onnx` + `vocab.txt` (+ optional
`tokenizer_config.json`) — model and tokenizer travel together so a swap is one env var
(EMBED_MODEL_PATH). The dim is validated against EMBED_DIM at load: a model whose hidden
size isn't 384 would silently corrupt the `vector(384)` column, so we raise instead and
load_embedder() falls back to the hash stub.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from libs.schema import EMBED_DIM
from libs.tokenize import WordPieceTokenizer

log = logging.getLogger("embed")

# Survivor texts are short (title + summary); 256 wordpieces is ample and bounds latency.
_MAX_LEN = 256


class OnnxEmbedder:
    dim = EMBED_DIM

    def __init__(self, path: str) -> None:
        import onnxruntime as ort  # heavy dep — lazy, as load_embedder() assumes

        d = Path(path)
        model_path = d / "model.onnx" if d.is_dir() else d
        tok_dir = d if d.is_dir() else d.parent
        self._tok = WordPieceTokenizer.from_dir(tok_dir, max_len=_MAX_LEN)
        self._sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self._input_names = {i.name for i in self._sess.get_inputs()}

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        _encs, batch = self._tok.encode_batch(texts)
        outputs = self._sess.run(None, batch.feed(self._input_names))
        hidden = np.asarray(outputs[0], dtype=np.float32)
        # last_hidden_state is (n, seq, H) → CLS pool the first token; some exports already
        # emit a pooled (n, H) sentence vector, in which case take it as-is.
        pooled = hidden[:, 0, :] if hidden.ndim == 3 else hidden
        if pooled.shape[1] != self.dim:
            raise ValueError(f"embedder hidden size {pooled.shape[1]} != EMBED_DIM {self.dim}")
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        norms[norms == 0] = 1.0  # a zero vector can't be normalized; leave it zero
        return (pooled / norms).astype(np.float32)
