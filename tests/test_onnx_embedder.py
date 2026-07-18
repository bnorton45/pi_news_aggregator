"""OnnxEmbedder tests (libs/embed) against a synthetic 384-dim model authored in-test.

The model is a single Gather (embedding lookup): output[n,0,:] = table[input_ids[n,0]],
i.e. the CLS row IS a token's embedding row — so we can assert exact pooling + L2 norm.
Skips cleanly where onnx/onnxruntime aren't installed (kept green on lean envs; CI's
enrich+retrain extras provide both).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from libs.schema import EMBED_DIM

ort = pytest.importorskip("onnxruntime")
onnx = pytest.importorskip("onnx")
from onnx import TensorProto, helper, numpy_helper  # noqa: E402

_TOKENS = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "quake", "calm"]


def _build(dir_: Path, hidden: int = EMBED_DIM) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "vocab.txt").write_text("\n".join(_TOKENS) + "\n", encoding="utf-8")
    rng = np.random.default_rng(0)
    table = rng.standard_normal((len(_TOKENS), hidden)).astype(np.float32)
    tbl = numpy_helper.from_array(table, name="table")
    x = helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["N", "S"])
    y = helper.make_tensor_value_info("last_hidden_state", TensorProto.FLOAT, ["N", "S", hidden])
    node = helper.make_node("Gather", ["table", "input_ids"], ["last_hidden_state"], axis=0)
    graph = helper.make_graph([node], "g", [x], [y], [tbl])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 9
    onnx.checker.check_model(model)
    (dir_ / "model.onnx").write_bytes(model.SerializeToString())


def test_encode_shape_and_normalization(tmp_path: Path) -> None:
    from libs.embed.onnx_embedder import OnnxEmbedder

    _build(tmp_path)
    emb = OnnxEmbedder(str(tmp_path))
    out = emb.encode(["quake in the bay", "all calm today"])
    assert out.shape == (2, EMBED_DIM)
    assert out.dtype == np.float32
    # every row L2-normalized (cosine-ready for pgvector `<=>`)
    np.testing.assert_allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-5)


def test_empty_batch_is_empty(tmp_path: Path) -> None:
    from libs.embed.onnx_embedder import OnnxEmbedder

    _build(tmp_path)
    assert OnnxEmbedder(str(tmp_path)).encode([]).shape == (0, EMBED_DIM)


def test_wrong_hidden_size_raises(tmp_path: Path) -> None:
    # A model whose hidden size isn't 384 would corrupt vector(384) — must raise so
    # load_embedder() falls back to the hash stub rather than serve bad vectors.
    from libs.embed.onnx_embedder import OnnxEmbedder

    _build(tmp_path, hidden=128)
    with pytest.raises(ValueError, match="EMBED_DIM"):
        OnnxEmbedder(str(tmp_path)).encode(["x"])


def test_load_embedder_falls_back_when_path_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from libs.embed import HashEmbedder, load_embedder

    monkeypatch.setenv("EMBED_MODEL_PATH", "/no/such/model/dir")
    assert isinstance(load_embedder(), HashEmbedder)


def test_load_embedder_uses_onnx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from libs.embed import load_embedder
    from libs.embed.onnx_embedder import OnnxEmbedder

    _build(tmp_path)
    monkeypatch.setenv("EMBED_MODEL_PATH", str(tmp_path))
    assert isinstance(load_embedder(), OnnxEmbedder)
