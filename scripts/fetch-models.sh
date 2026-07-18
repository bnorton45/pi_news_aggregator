#!/usr/bin/env bash
# Fetch the real ONNX models (embedder + NER) into the gitignored ./models dir for the
# compose dev loop (PLAN §6.3 step 3/4). ZERO-EGRESS BY DESIGN: nothing downloads models
# at runtime — this is an explicit, human-run, sha256-verified fetch. The compose stack
# volume-mounts ./models read-only and sets EMBED_MODEL_PATH / NER_MODEL_PATH; if a model
# is absent or fails to load, enrich falls back to the hash/no-op stubs (CI stays green
# with no models on the runner).
#
# Integrity: every file is checked against a pinned sha256. On a fresh checkout the shas
# are empty — run once with FETCH_MODELS_TOFU=1 to trust-on-first-use; the script prints
# the sha256 of each file for you to paste into the SHA256 map below and commit. After
# that the fetch is fail-closed on any tampering.
#
# The NER model is PROVISIONAL (PLAN §11 red-line, finalized by Pi measurement); the
# embedder (bge-small-en-v1.5, 384-dim) is decided. Both use vocab.txt WordPiece.
set -euo pipefail
cd "$(dirname "$0")/.."

DEST="${MODELS_DIR:-models}"
TOFU="${FETCH_MODELS_TOFU:-0}"

# name|dest-subdir|url  — Xenova ONNX ports carry vocab.txt + tokenizer_config.json, which
# the pure-python WordPiece tokenizer (libs/tokenize) needs. REV pinned to a commit for a
# reproducible fetch (override to re-pin); the sha256 map below is the integrity anchor.
BGE_REV="${BGE_REV:-ea104dacec62c0de699686887e3f920caeb4f3e3}"
NER_REV="${NER_REV:-24c7e5aba9ae350923357a6f0b92571be34037ec}"
BGE_BASE="https://huggingface.co/Xenova/bge-small-en-v1.5/resolve/${BGE_REV}"
NER_BASE="https://huggingface.co/Xenova/bert-base-NER/resolve/${NER_REV}"

# file-path (relative to $DEST) -> source URL. We ship ONE precision — **int8** (the
# upstream `model_quantized.onnx`, saved locally as model.onnx so the dir convention holds).
# int8 is 2.4x faster, 4x smaller (~360MB less enrich RAM), and measured decision-equivalent
# to fp32 (docs/pi-throughput-findings.md: cos 0.996, 0 clustering flips at theta>=0.7, NER
# identical) — the memory win is worth the marginal borderline noise. fp32 was dropped; it is
# restorable from `onnx/model.onnx` in the same repo if int8 NER noise ever bites on real data.
declare -A FILES=(
  ["bge-small/model.onnx"]="${BGE_BASE}/onnx/model_quantized.onnx"
  ["bge-small/vocab.txt"]="${BGE_BASE}/vocab.txt"
  ["bge-small/tokenizer_config.json"]="${BGE_BASE}/tokenizer_config.json"
  ["ner/model.onnx"]="${NER_BASE}/onnx/model_quantized.onnx"
  ["ner/vocab.txt"]="${NER_BASE}/vocab.txt"
  ["ner/tokenizer_config.json"]="${NER_BASE}/tokenizer_config.json"
  ["ner/config.json"]="${NER_BASE}/config.json"
)

# Pinned sha256 (fill via a FETCH_MODELS_TOFU=1 run, then commit). Empty ⇒ not yet pinned.
# These are public model-artifact checksums, not secrets — the allowlist pragma silences
# detect-secrets' high-entropy-hex heuristic.
declare -A SHA256=(
  ["bge-small/model.onnx"]="6c9c6101a956d62dfb5e7190c538226c0c5bb9cb27b651234b6df063ee7dbfe4"            # pragma: allowlist secret
  ["bge-small/vocab.txt"]="07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3"              # pragma: allowlist secret
  ["bge-small/tokenizer_config.json"]="9261e7d79b44c8195c1cada2b453e55b00aeb81e907a6664974b4d7776172ab3"  # pragma: allowlist secret
  ["ner/model.onnx"]="caaee70a5518ec7f9e46e5308fcc9263a8c227703a9ce46cf61c69a552349648"                  # pragma: allowlist secret
  ["ner/vocab.txt"]="eeaa9875b23b04b4c54ef759d03db9d1ba1554838f8fb26c5d96fa551df93d02"                    # pragma: allowlist secret
  ["ner/tokenizer_config.json"]="5be1a180e9badb4811a6c31502d70fb35a085af5457982937419c42d7530bae6"        # pragma: allowlist secret
  ["ner/config.json"]="a73a2eccc921bbdea95a94b49a157d3694b5c2abbae7a6f3000e14404a9c31a8"                  # pragma: allowlist secret
)

fail=0
for rel in "${!FILES[@]}"; do
  url="${FILES[$rel]}"
  out="${DEST}/${rel}"
  want="${SHA256[$rel]}"
  mkdir -p "$(dirname "$out")"
  echo ">> fetching ${rel}"
  curl -fSL --retry 3 -o "$out" "$url"
  got="$(sha256sum "$out" | cut -d' ' -f1)"
  if [ -z "$want" ]; then
    if [ "$TOFU" = "1" ]; then
      echo "   TOFU sha256 ${rel}: ${got}   <-- paste into SHA256 map and commit"
    else
      echo "   ERROR: ${rel} has no pinned sha256. Re-run with FETCH_MODELS_TOFU=1 to pin." >&2
      fail=1
    fi
  elif [ "$want" != "$got" ]; then
    echo "   ERROR: sha256 mismatch for ${rel}" >&2
    echo "     want ${want}" >&2
    echo "     got  ${got}" >&2
    rm -f "$out"
    fail=1
  else
    echo "   ok (sha256 verified)"
  fi
done

if [ "$fail" != "0" ]; then
  echo "!! fetch incomplete — see errors above" >&2
  exit 1
fi

# NER labels come from config.json's id2label (OnnxNer reads it); no labels.txt needed.
echo
echo "Models in ./${DEST} (int8 — the only shipped precision)."
echo "  EMBED_MODEL_PATH=/models/bge-small   NER_MODEL_PATH=/models/ner"
echo "NOTE: switching stub<->real vectors changes the embedding space — the pgvector ANN"
echo "index mixes them otherwise. Start clean: docker compose down -v."
