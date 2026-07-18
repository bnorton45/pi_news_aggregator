# Pi-throughput findings (dev-box benchmark, 2026-07-09)

De-risking "can 3× Raspberry Pi 5 hold the throughput?" ahead of the Phase-0b hardware.
Measured on an i9-12900K (24 logical, DDR5) — an **optimistic upper bound** for a Pi 5:
onnxruntime uses AVX2 on x86 vs only NEON on ARM, and a Golden Cove core ≫ a Cortex-A76,
so absolute ARM numbers must still be measured at 0b (arm64-smoke proves it *runs*, not its
speed). Scripts: benchmarks were run against the real `OnnxEmbedder`/`OnnxNer`/`OllamaClient`
code paths. Governor social budget = 400k/day ≈ **4.63 items/s** (`services/enrich/governor.py`).

## 1. Firehose path (bge-small embed + bert-base-NER) — comfortable margin
Combined NER+embed per survivor, x86:

| precision | 1 thread | 4 threads |
|-----------|----------|-----------|
| fp32      | 13.3/s   | 36.9/s    |
| **int8**  | **34.8/s** | **83.7/s** |

- Quantization ≈ **2.4× faster, 4× smaller** (bge 127→33 MB, NER 412→104 MB). NER dominates cost.
- Even 1-thread int8 clears the 4.63/s budget **7.5×**. De-rated hard for ARM (~6–10×/core),
  one Pi 5 node ≈ 8–14/s on int8 — still above budget, with a 2nd compute node behind it.
- The firehose path is **not** the risk.

## 2. fp32 vs int8 QUALITY — decision-equivalent
Only downstream *decisions* matter (clustering: cosine vs θ; NER: which entities). Measured on
a paraphrase-cluster + filler corpus:

- Embedder: per-text `cos(fp32, int8)` = **0.9964** (min 0.9950); pairwise-similarity
  correlation **0.9983**; clustering-decision flips at θ=0.7 / 0.8 = **0 / 0** (only borderline
  pairs flip, and only at a low θ=0.6: 2/40). Paraphrase separation gap fp32 0.210 vs int8 0.205.
- NER: fp32 and int8 extracted the **identical** entity set (Jaccard 1.000, 0/16 texts differed).

**Conclusion:** fp32's precision edge is below the decision noise floor here. int8 is
decision-equivalent **and** saves resident model RAM (measured **−356 MiB enrich RSS**: 633→277 MiB
with a live A/B) + energy/thermal. (Model quantization does *not* change the stored embedding —
output is still fp32 384-dim; index size is governed separately by pgvector `halfvec`.)
**DECIDED 2026-07-09: ship int8 ONLY, everywhere (dev + Pi); fp32 dropped — the memory win is worth
the marginal noise.** Caveat: small curated corpus; a messier real firehose could surface a
few borderline NER diffs → *0b spot-check* on real ARM data; fp32 is one-line restorable if needed.

## 3. LLM claim extraction — qwen3:4b is BROKEN for this task; use a small model
claimx runs Ollama non-thinking (`think=False`) with "output only the claim". Measured:

- **qwen3:4b — UNUSABLE.** Emits its reasoning *into `content`* (no separate `thinking` field)
  regardless of `think=False`, `/no_think`, or a `num_predict` cap → 419–2650 tokens of
  "Hmm, the user wants me to extract…" for a one-liner. **40–280 s/claim** on x86 @ ~10 tok/s →
  ~635 s/claim de-rated to a Pi. Both slow *and* wrong output.
- **qwen3:1.7b — CORRECT + FAST.** Clean one-line claims (8–20 tok), **0.9 s/claim** x86 @ 25 tok/s
  → ~4 s/claim Pi (÷4.5 memory-bandwidth de-rate) → **~860 claims/hr/replica**. Fully viable.

**Conclusion:** claim extraction is simple/extractive — it does **not** need (and is actively
harmed by) a 4B reasoner. The §12 "run non-thinking by default" mitigation is **insufficient** for
the 4B here. Claim extraction uses a **small non-reasoning model (qwen3:1.7b class)**; the 4B is
retained for summarization / entity resolution / the thinking-enabled analyst brief.

## 4. Config action item for 0b — cap onnxruntime intra-op threads
The code sets **no** ORT thread config anywhere (`grep intra_op|inter_op|num_thread|SessionOptions|OMP_NUM_THREADS` over `services/`+`libs/` is empty), so onnxruntime defaults to grabbing **every visible core** for intra-op parallelism on a single inference. On the dev box this is why compose pins all 24 cores while enrich is doing `<1 item/s` — one small embed fanned across the whole machine, *not* real load (compose also sets no CPU limits — `docker-compose*.yml` has no `cpus`/`deploy.resources`). The Helm values **do** cap every pod (`values.yaml`), so k8s CFS will throttle an over-eager pool into its cgroup quota — but that's thread thrash, not clean scheduling.

**0b bootstrap item:** set `intra_op_num_threads` explicitly on the Pi (via `SessionOptions` in `OnnxEmbedder`/`OnnxNer`, or an env knob) to match the pod's CPU limit (e.g. 1–2 on a 4-core node) rather than leaning on CFS to clip a pool sized for all 4 cores. Cheap, avoids scheduler thrash under the governor. Not a dev-box change — only matters once pods are CPU-limited on real hardware.

## Net answer
3 Pi 5s can hold it **if** (a) int8 quantized ONNX models and (b) a small claim model (not the 4B).
The admission governor guarantees graceful degradation (sheds the low-relevance tail, never
crashes) regardless. Remaining true unknown = absolute ARM/NEON throughput → Phase-0b measurement.
