"""BERT WordPiece tokenizer, offsets and all, in pure python (PLAN §6.3 step 3/4).

The classic two-stage BERT pipeline: a BasicTokenizer (clean → optional lowercase/accent
strip → split on whitespace and punctuation, isolating CJK) followed by greedy
longest-match WordPiece over a vocab.txt. It is intentionally faithful to
`transformers.BertTokenizer` for the uncased (bge-small) and cased (NER) cases we ship.

Offsets are the reason this is hand-written rather than a one-liner: NER decodes labels
back to entity **text**, so every subword must carry the char span of its parent *word* in
the ORIGINAL string. We track offsets at word granularity (not subword) on purpose —
entity boundaries in token-classification are word-level, and accent-stripping can change
a word's char count, so a proportional sub-span would be wrong. Merging B/I subwords by
`min(start)..max(end)` over their parent-word spans reconstructs the exact source substring.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# BERT specials. A vocab may name them differently, but these are the near-universal
# defaults for bert-base(-uncased) and the NER heads built on it.
CLS = "[CLS]"
SEP = "[SEP]"
PAD = "[PAD]"
UNK = "[UNK]"

_MAX_CHARS_PER_WORD = 100  # HF default: words longer than this collapse to a single [UNK]


@dataclass(frozen=True)
class Encoding:
    """One tokenized text.

    `ids` include the leading [CLS] and trailing [SEP]. `word_ids[i]` is the index of the
    source word token i came from, or None for a special/pad token. `word_spans[w]` is the
    (start, end) char span of word w in the ORIGINAL text — used by NER to slice entities.
    """

    ids: list[int]
    word_ids: list[int | None]
    word_spans: list[tuple[int, int]]


def _is_control(ch: str) -> bool:
    if ch in ("\t", "\n", "\r"):
        return False
    return unicodedata.category(ch).startswith("C")


def _is_whitespace(ch: str) -> bool:
    if ch in (" ", "\t", "\n", "\r"):
        return True
    return unicodedata.category(ch) == "Zs"


def _is_punctuation(ch: str) -> bool:
    cp = ord(ch)
    # ASCII ranges BERT treats as punctuation even though unicodedata does not (e.g. `$`).
    if (33 <= cp <= 47) or (58 <= cp <= 64) or (91 <= cp <= 96) or (123 <= cp <= 126):
        return True
    return unicodedata.category(ch).startswith("P")


def _is_cjk(cp: int) -> bool:
    # CJK Unified Ideographs (+ extensions/compat) — BERT isolates these as single tokens.
    return (
        0x4E00 <= cp <= 0x9FFF
        or 0x3400 <= cp <= 0x4DBF
        or 0x20000 <= cp <= 0x2A6DF
        or 0x2A700 <= cp <= 0x2B73F
        or 0x2B740 <= cp <= 0x2B81F
        or 0x2B820 <= cp <= 0x2CEAF
        or 0xF900 <= cp <= 0xFAFF
        or 0x2F800 <= cp <= 0x2FA1F
    )


class WordPieceTokenizer:
    def __init__(
        self,
        vocab: dict[str, int],
        *,
        do_lower_case: bool = True,
        strip_accents: bool | None = None,
        max_len: int = 256,
    ) -> None:
        self.vocab = vocab
        self.do_lower_case = do_lower_case
        # HF convention: when unset, accent-stripping follows the lowercasing flag.
        self.strip_accents = do_lower_case if strip_accents is None else strip_accents
        self.max_len = max_len
        self.cls_id = vocab[CLS]
        self.sep_id = vocab[SEP]
        self.pad_id = vocab.get(PAD, 0)
        self.unk_id = vocab[UNK]

    @classmethod
    def from_dir(cls, path: str | Path, *, max_len: int = 256) -> WordPieceTokenizer:
        """Load `vocab.txt` (line number = id) and, if present, `tokenizer_config.json`
        for the do_lower_case / strip_accents flags (cased NER vs uncased bge differ)."""
        d = Path(path)
        vocab: dict[str, int] = {}
        with (d / "vocab.txt").open(encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                # id == line number, always (keyed by enumerate index, not len) so a skipped
                # blank line can't shift ids. rstrip("\n") only: a literal space-like glyph
                # token is preserved; only truly empty lines (e.g. a trailing newline) drop.
                tok = line.rstrip("\n")
                if tok == "":
                    continue
                vocab[tok] = i
        do_lower = True
        strip: bool | None = None
        cfg_path = d / "tokenizer_config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            do_lower = bool(cfg.get("do_lower_case", True))
            if "strip_accents" in cfg and cfg["strip_accents"] is not None:
                strip = bool(cfg["strip_accents"])
        return cls(vocab, do_lower_case=do_lower, strip_accents=strip, max_len=max_len)

    # --- stage 1: basic tokenization into (word, start, end) over the ORIGINAL text -------

    def _basic_tokens(self, text: str) -> list[tuple[str, int, int]]:
        """Split into word tokens, each with its char span in `text`. Punctuation and CJK
        become their own tokens. Lowercasing/accent-stripping is applied to the token *text*
        used for vocab lookup, but spans always index the original string."""
        words: list[tuple[str, int, int]] = []
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if _is_whitespace(ch) or _is_control(ch):
                i += 1
                continue
            if _is_punctuation(ch) or _is_cjk(ord(ch)):
                words.append((self._normalize(ch), i, i + 1))
                i += 1
                continue
            # Consume a run of "word" chars up to the next whitespace/punct/control/CJK.
            start = i
            while i < n:
                c = text[i]
                if _is_whitespace(c) or _is_control(c) or _is_punctuation(c) or _is_cjk(ord(c)):
                    break
                i += 1
            words.append((self._normalize(text[start:i]), start, i))
        return words

    def _normalize(self, token: str) -> str:
        if self.do_lower_case:
            token = token.lower()
        if self.strip_accents:
            token = "".join(
                c for c in unicodedata.normalize("NFD", token) if unicodedata.category(c) != "Mn"
            )
        return token

    # --- stage 2: greedy longest-match WordPiece ------------------------------------------

    def _wordpiece(self, token: str) -> list[int]:
        if len(token) > _MAX_CHARS_PER_WORD:
            return [self.unk_id]
        chars = list(token)
        out: list[int] = []
        start = 0
        while start < len(chars):
            end = len(chars)
            cur: int | None = None
            while start < end:
                sub = "".join(chars[start:end])
                if start > 0:
                    sub = "##" + sub
                if sub in self.vocab:
                    cur = self.vocab[sub]
                    break
                end -= 1
            if cur is None:  # no vocab prefix matched → whole word is [UNK]
                return [self.unk_id]
            out.append(cur)
            start = end
        return out

    # --- public API -----------------------------------------------------------------------

    def encode(self, text: str) -> Encoding:
        ids = [self.cls_id]
        word_ids: list[int | None] = [None]
        word_spans: list[tuple[int, int]] = []
        budget = self.max_len - 2  # reserve [CLS] and [SEP]
        for word_index, (word, s, e) in enumerate(self._basic_tokens(text)):
            piece_ids = self._wordpiece(word)
            if len(ids) - 1 + len(piece_ids) > budget:
                break  # truncate on a word boundary — never split a word across the limit
            word_spans.append((s, e))
            for pid in piece_ids:
                ids.append(pid)
                word_ids.append(word_index)
        ids.append(self.sep_id)
        word_ids.append(None)
        return Encoding(ids=ids, word_ids=word_ids, word_spans=word_spans)

    def encode_batch(self, texts: list[str]) -> tuple[list[Encoding], _BatchArrays]:
        """Encode + right-pad to the batch max. Returns the per-text Encodings (for NER
        decode) alongside int64 input_ids / attention_mask / token_type_ids arrays."""
        import numpy as np

        encs = [self.encode(t) for t in texts]
        width = max((len(e.ids) for e in encs), default=1)
        n = len(encs)
        input_ids = np.full((n, width), self.pad_id, dtype=np.int64)
        attention = np.zeros((n, width), dtype=np.int64)
        for i, e in enumerate(encs):
            L = len(e.ids)
            input_ids[i, :L] = e.ids
            attention[i, :L] = 1
        token_type = np.zeros((n, width), dtype=np.int64)
        return encs, _BatchArrays(input_ids, attention, token_type)


@dataclass(frozen=True)
class _BatchArrays:
    input_ids: object  # np.ndarray[int64] (n, width)
    attention_mask: object
    token_type_ids: object

    def feed(self, input_names: set[str]) -> dict[str, object]:
        """The subset of {input_ids, attention_mask, token_type_ids} the ONNX graph names
        — some exports omit token_type_ids, so only feed inputs the session declares."""
        avail = {
            "input_ids": self.input_ids,
            "attention_mask": self.attention_mask,
            "token_type_ids": self.token_type_ids,
        }
        return {name: arr for name, arr in avail.items() if name in input_names}
