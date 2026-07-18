"""OnnxNer tests (libs/ner) against a synthetic token-classifier authored in-test.

The model is a Gather over a per-token logit table, so a token id deterministically emits a
chosen CoNLL label. That exercises the real path: WordPiece → argmax → BIO decode → entity
text sliced from the original string. Skips where onnx/onnxruntime are absent.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from libs.schema import EntityType

ort = pytest.importorskip("onnxruntime")
onnx = pytest.importorskip("onnx")
from onnx import TensorProto, helper, numpy_helper  # noqa: E402

# cased vocab (like bert-base-NER); ids are line numbers
_TOKENS = [
    "[PAD]",
    "[UNK]",
    "[CLS]",
    "[SEP]",
    "Alice",
    "visited",
    "Tokyo",
    "yesterday",
    "New",
    "York",
]
_LABELS = ["O", "B-MISC", "I-MISC", "B-PER", "I-PER", "B-ORG", "I-ORG", "B-LOC", "I-LOC"]
# token id -> winning label id
_WIN = {4: 3, 6: 7, 8: 7, 9: 8}  # Alice=B-PER, Tokyo=B-LOC, New=B-LOC, York=I-LOC


def _build(dir_: Path) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "vocab.txt").write_text("\n".join(_TOKENS) + "\n", encoding="utf-8")
    (dir_ / "tokenizer_config.json").write_text(json.dumps({"do_lower_case": False}))
    (dir_ / "config.json").write_text(json.dumps({"id2label": dict(enumerate(_LABELS))}))
    table = np.zeros((len(_TOKENS), len(_LABELS)), dtype=np.float32)
    for tid, lab in _WIN.items():
        table[tid, lab] = 5.0  # dominate the argmax
    tbl = numpy_helper.from_array(table, name="table")
    x = helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["N", "S"])
    y = helper.make_tensor_value_info("logits", TensorProto.FLOAT, ["N", "S", len(_LABELS)])
    node = helper.make_node("Gather", ["table", "input_ids"], ["logits"], axis=0)
    graph = helper.make_graph([node], "g", [x], [y], [tbl])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 9
    onnx.checker.check_model(model)
    (dir_ / "model.onnx").write_bytes(model.SerializeToString())


def test_extracts_person_and_place(tmp_path: Path) -> None:
    from libs.ner.onnx_ner import OnnxNer

    _build(tmp_path)
    ents = OnnxNer(str(tmp_path)).extract("Alice visited Tokyo yesterday")
    got = {(e.text, e.type) for e in ents}
    assert got == {("Alice", EntityType.PERSON), ("Tokyo", EntityType.PLACE)}


def test_multiword_entity_spans_the_whole_phrase(tmp_path: Path) -> None:
    from libs.ner.onnx_ner import OnnxNer

    _build(tmp_path)
    ents = OnnxNer(str(tmp_path)).extract("New York")
    assert [(e.text, e.type) for e in ents] == [("New York", EntityType.PLACE)]


def test_ner_leaves_geo_to_the_gazetteer(tmp_path: Path) -> None:
    from libs.ner.onnx_ner import OnnxNer

    _build(tmp_path)
    ents = OnnxNer(str(tmp_path)).extract("Tokyo")
    assert ents and all(e.geo is None for e in ents)


def test_blank_text_is_no_entities(tmp_path: Path) -> None:
    from libs.ner.onnx_ner import OnnxNer

    _build(tmp_path)
    assert OnnxNer(str(tmp_path)).extract("   ") == []


def test_load_ner_falls_back_when_path_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from libs.ner import NoOpNer, load_ner

    monkeypatch.setenv("NER_MODEL_PATH", "/no/such/model/dir")
    assert isinstance(load_ner(), NoOpNer)


def test_load_ner_uses_onnx_when_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from libs.ner import load_ner
    from libs.ner.onnx_ner import OnnxNer

    _build(tmp_path)
    monkeypatch.setenv("NER_MODEL_PATH", str(tmp_path))
    assert isinstance(load_ner(), OnnxNer)
