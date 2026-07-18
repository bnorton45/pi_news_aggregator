"""ONNX NER token-classifier (PLAN §6.3 step 4) — the real path behind load_ner().

A small BERT token-classification head (CoNLL PER/ORG/LOC/MISC) run on admitted survivors
only (≤400k/day, never the firehose). WordPiece-tokenize → per-token logits → take the
label of each word's FIRST subword (HF alignment convention) → BIO-decode into entity
spans → slice the entity **text** back out of the original string via the word offsets.

`path` is a directory: `model.onnx` + `vocab.txt` (+ optional `tokenizer_config.json`,
`labels.txt`). Labels are read from `labels.txt` (line number = class id) or a HF
`config.json` `id2label`, defaulting to the dslim/bert-base-NER CoNLL-2003 order.

Geo is left None: coarse geo comes from the gazetteer (libs/gazetteer resolves lat/lon
from a curated table); a token-classifier only says "this span is a place", not *where*.
`merge_entities` dedups NER output against the gazetteer's, so a place the gazetteer knows
keeps its geo while NER adds the ones the gazetteer misses.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from libs.schema import MAX_TEXT_LEN, Entity, EntityType
from libs.tokenize import WordPieceTokenizer

log = logging.getLogger("ner")

_MAX_LEN = 256
_MAX_ENTITY_TEXT = 256  # Entity.text max_length (libs/schema)

# dslim/bert-base-NER id2label (CoNLL-2003) — the fallback when the model dir ships no
# explicit label map. The real pick is finalized by Pi measurement.
_DEFAULT_LABELS = ["O", "B-MISC", "I-MISC", "B-PER", "I-PER", "B-ORG", "I-ORG", "B-LOC", "I-LOC"]

# CoNLL tag family → our Entity taxonomy (libs/schema EntityType).
_TAG_TO_TYPE = {
    "PER": EntityType.PERSON,
    "ORG": EntityType.ORG,
    "LOC": EntityType.PLACE,
    "MISC": EntityType.OTHER,
}


def _load_labels(d: Path) -> list[str]:
    labels_txt = d / "labels.txt"
    if labels_txt.exists():
        return [ln.rstrip("\n") for ln in labels_txt.read_text(encoding="utf-8").splitlines() if ln]
    cfg = d / "config.json"
    if cfg.exists():
        id2label = json.loads(cfg.read_text(encoding="utf-8")).get("id2label")
        if id2label:
            return [id2label[str(i)] for i in range(len(id2label))]
    log.warning("NER: no labels.txt/config.json in %s; assuming dslim/bert-base-NER order", d)
    return list(_DEFAULT_LABELS)


class OnnxNer:
    def __init__(self, path: str) -> None:
        import onnxruntime as ort  # heavy dep — lazy, as load_ner() assumes

        d = Path(path)
        model_path = d / "model.onnx" if d.is_dir() else d
        tok_dir = d if d.is_dir() else d.parent
        self._tok = WordPieceTokenizer.from_dir(tok_dir, max_len=_MAX_LEN)
        self._labels = _load_labels(tok_dir)
        self._sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self._input_names = {i.name for i in self._sess.get_inputs()}

    def extract(self, text: str) -> list[Entity]:
        if not text.strip():
            return []
        import numpy as np

        encs, batch = self._tok.encode_batch([text[:MAX_TEXT_LEN]])
        enc = encs[0]
        (logits,) = self._sess.run(None, batch.feed(self._input_names))
        pred = np.asarray(logits)[0].argmax(axis=-1)  # (seq,) class id per token position

        # Reduce to one label per source word: the first subword's prediction (HF convention).
        word_label: dict[int, str] = {}
        for pos, wid in enumerate(enc.word_ids):
            if wid is None or wid in word_label:
                continue
            cid = int(pred[pos])
            word_label[wid] = self._labels[cid] if 0 <= cid < len(self._labels) else "O"

        return self._decode_bio(text, enc.word_spans, word_label)

    def _decode_bio(
        self, text: str, word_spans: list[tuple[int, int]], word_label: dict[int, str]
    ) -> list[Entity]:
        entities: list[Entity] = []
        cur_type: str | None = None
        cur_start = cur_end = 0
        seen: set[tuple[str, EntityType]] = set()

        def flush() -> None:
            nonlocal cur_type
            if cur_type is None:
                return
            etype = _TAG_TO_TYPE.get(cur_type, EntityType.OTHER)
            span_text = text[cur_start:cur_end].strip()[:_MAX_ENTITY_TEXT]
            key = (span_text, etype)
            if span_text and key not in seen:
                seen.add(key)
                entities.append(Entity(text=span_text, type=etype))  # geo resolved by gazetteer
            cur_type = None

        for wid in range(len(word_spans)):
            label = word_label.get(wid, "O")
            prefix, _, tag = label.partition("-")
            if prefix == "B" or (prefix == "I" and tag != cur_type):
                # New entity: a B-, or an I- whose type doesn't continue the current span
                # (lenient — some taggers open with I-).
                flush()
                cur_type = tag
                cur_start, cur_end = word_spans[wid]
            elif prefix == "I" and tag == cur_type:
                cur_end = word_spans[wid][1]  # extend the open span
            else:  # "O" or unrecognized → close any open entity
                flush()
        flush()
        return entities
