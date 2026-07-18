"""Pure-python WordPiece tokenizer tests (libs/tokenize) — no model files needed."""

from __future__ import annotations

from libs.tokenize import WordPieceTokenizer

_VOCAB = {
    "[PAD]": 0,
    "[UNK]": 1,
    "[CLS]": 2,
    "[SEP]": 3,
    "hello": 4,
    "world": 5,
    "##ing": 6,
    "test": 7,
    ",": 8,
    "tokyo": 9,
    "café": 10,
    "cafe": 11,
}


def _tok(**kw: object) -> WordPieceTokenizer:
    return WordPieceTokenizer(dict(_VOCAB), **kw)  # type: ignore[arg-type]


def test_specials_wrap_the_sequence() -> None:
    enc = _tok().encode("hello world")
    assert enc.ids[0] == _VOCAB["[CLS]"]
    assert enc.ids[-1] == _VOCAB["[SEP]"]
    assert enc.ids[1:-1] == [_VOCAB["hello"], _VOCAB["world"]]
    assert enc.word_ids == [None, 0, 1, None]


def test_lowercasing_is_default() -> None:
    # "Hello" must match the lowercase vocab entry when do_lower_case (default) is on.
    assert _tok().encode("Hello").ids[1] == _VOCAB["hello"]


def test_offsets_index_the_original_text() -> None:
    text = "Hello, world"
    enc = _tok().encode(text)
    # punctuation splits into its own token; spans slice the ORIGINAL (cased) string
    assert enc.word_spans == [(0, 5), (5, 6), (7, 12)]
    assert [text[s:e] for s, e in enc.word_spans] == ["Hello", ",", "world"]


def test_wordpiece_continuation() -> None:
    enc = _tok().encode("testing")
    assert enc.ids[1:-1] == [_VOCAB["test"], _VOCAB["##ing"]]
    # both subwords map back to the single source word 0
    assert enc.word_ids[1:-1] == [0, 0]


def test_unknown_word_is_unk() -> None:
    assert _tok().encode("zzqq").ids[1] == _VOCAB["[UNK]"]


def test_accent_stripping_follows_lowercasing() -> None:
    # uncased default strips accents → "café" matches the plain "cafe" entry
    assert _tok().encode("Café").ids[1] == _VOCAB["cafe"]
    # cased tokenizer keeps the accent → matches the "café" entry instead
    assert _tok(do_lower_case=False).encode("café").ids[1] == _VOCAB["café"]


def test_batch_pads_to_max_width() -> None:
    _encs, batch = _tok().encode_batch(["hello", "hello world testing"])
    ids = batch.input_ids
    assert ids.shape[0] == 2
    assert ids.shape[1] == 6  # longest row: CLS hello world test ##ing SEP
    # attention mask marks real tokens; padded tail is zero
    assert batch.attention_mask[0].sum() == 3  # CLS hello SEP
    assert batch.attention_mask[1].sum() == 6  # CLS hello world test ##ing SEP


def test_feed_filters_to_declared_inputs() -> None:
    _encs, batch = _tok().encode_batch(["hello"])
    fed = batch.feed({"input_ids"})
    assert set(fed) == {"input_ids"}
    fed2 = batch.feed({"input_ids", "attention_mask", "token_type_ids"})
    assert set(fed2) == {"input_ids", "attention_mask", "token_type_ids"}


def test_truncation_reserves_specials() -> None:
    t = _tok(max_len=4)  # room for CLS + 2 wordpieces + SEP
    enc = t.encode("hello world tokyo")
    assert len(enc.ids) == 4
    assert enc.ids[0] == _VOCAB["[CLS]"] and enc.ids[-1] == _VOCAB["[SEP]"]
