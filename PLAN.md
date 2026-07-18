# OSINT Developing-Story Aggregator — Build-Ready Plan

---

## 1. Goal & principles

**Goal:** Surface *developing* stories and information from open sources that may or may not
reach mainstream news — fast, but **only after corroboration** so we don't amplify noise or
disinformation.

**Non-goals / hard rules:**
- **Not a paywall bypass.** Mainstream outlets (BBC, etc.) are never scraped for article
  content. They are used *only* as a **presence baseline** ("has this reached mainstream
  yet?") via free headline/metadata feeds. The product's value is the *gap*.
- **Corroboration-first.** Nothing is flagged "real" without **N independent sources** or a
  **primary record** backing a social claim. Raw feeds are treated as untrusted by default.
- **Ephemeral by design.** Hard 5-day data wall, no exceptions, no archival backups.
- **Compromise-resilient.** Any single component may be popped; the system contains the blast.
- **Single user, global scope.**

---

## 2. Hardware & cluster topology

- **3× Raspberry Pi 5, 16GB RAM each** (48GB total).
- **Enclosure (decided 2026-07-02): 3× SunFounder Pironman 5 (standard, NOT the MAX).** Its single
  M.2 slot (direct to the Pi's PCIe FFC) takes the NVMe; **force PCIe Gen3** in config. Tower
  cooler + dual fans satisfy the active-cooling requirement; safe-shutdown button aids the state
  node. **No Hailo-8L** (slot is taken by the SSD, and it can't run any model in this design —
  the 4B and the ONNX encoders stay on CPU). **RGB off** (headless; power/noise). The MAX was
  rejected: its second slot adds nothing here and its ASM1182e switch downgrades the lane to Gen2.
  ⚠ Fans/OLED/safe-shutdown need SunFounder's `pironman5` daemon — **third-party root systemd
  service (I2C/GPIO) on every node**: install a *pinned* version at provision time (0b runbook),
  no auto-update, verify zero egress. Always flash a clean OS — never a vendor image.
- **1× 1TB NVMe (Samsung 990 EVO) per node** in the Pironman M.2 slot. **Boot from NVMe**; SD card
  is recovery-only. Active cooling required (NVMe + Pi 5 under sustained DB load runs hot).
- **k3s, 3-server HA** with embedded etcd (`--cluster-init` + join). Survives loss of any one
  node. etcd on NVMe handles the fsync load fine.
- **OS:** Ubuntu Server 24.04 arm64 or RPi OS Lite 64-bit (Bookworm). Enable memory cgroups in
  `cmdline.txt`. Proper 5V/5A USB-C PD power per node; UPS recommended.
- **Capacity:** ~3TB raw NVMe, ~1.5TB usable at 2× replication — vastly more than a 5-day
  window needs (intentional headroom = low wear).
- **Embed-budget invariant:** the binding resource is **HNSW index RAM**, not disk. Social-class
  survivors are capped at **≤400k/day**; at the 5-day window that is ~2.0M live vectors ≈ ~8GB index
  — comfortable on a 16GB node alongside Postgres buffers + OS, leaving cushion for transient HNSW
  build/maintenance spikes. Conservative by choice (the marginal tail above this is lowest-relevance;
  an OOM → failover churn is worse than shedding chatter); raise on evidence after Phase 0b measures
  the real per-vector footprint (`halfvec` storage roughly halves it). Held automatically by the
  adaptive admission controller (§6.3a); authoritative/primary sources are exempt. The HNSW index now
  co-resides with NATS JetStream + Prometheus/Grafana on the **state** node (below) — still comfortable
  at the 400k/day cap (~8GB index + Postgres buffers + NATS + monitoring + OS on 16GB).

### Workload placement
The enrichment LLM is now **local** — Ollama serving **Qwen3-4B** (no Anthropic, zero internet egress,
§6.3). The 4B is confined to **Story-level, low-volume** work (§6.3 step 4), so it is **low duty
cycle** — bursty and idle most of the time, *not* a steady 3-core hog. That changes the placement math:
the compute nodes have spare CPU between 4B bursts, which is exactly where the high-volume firehose
classifier lives. Two LLM replicas run on the **compute** nodes for HA + burst; stateful + monitoring
pieces consolidate on the **state** node.
```
node-1  role=compute "LLM-A"   k3s server+etcd+Longhorn · Ollama Qwen3-4B replica #1 ·
                               firehose-classify + borderline-small-LLM workers · ingesters (~5/s)
node-2  role=compute "LLM-B"   k3s server+etcd+Longhorn · Ollama Qwen3-4B replica #2 ·
                               firehose-classify + embedder (bge-small) workers (pull queue, write vec)
node-3  role=state   "State"   k3s server+etcd+Longhorn · Postgres+pgvector (vectors AND the
                               seen/dedup memory) · NATS JetStream · Prometheus+Grafana ·
                               process/cluster/score/API workers · Flux controllers — NO LLM
```

**k3s mechanics that make this stick:**
- **Pin the two 4B replicas apart** — `podAntiAffinity` (required-during-scheduling, by replica label)
  so k3s never co-schedules both onto one node: one survives a node loss; both up = burst throughput.
- **CPU priority, not core-pinning, between classifier and 4B.** Because the 4B is low duty
  cycle, do **not** reserve 3 cores for it. Give the **firehose-classify workers a guaranteed CPU
  `request`** (they're the binding real-time constraint — losing firehose classification loses data)
  and give Ollama a **low `request` + high `limit`** so it *bursts* into idle headroom but **yields**
  to the classifier under contention. A delayed Story summary is fine; dropped firehose classification
  is not. Extreme storms fall back to the §6.3a JetStream buffer + high-water sampling.
- **State stays on node-3 but can fail over.** Use `nodeAffinity: preferred role=state`
  (high weight) — **not** a hard `nodeSelector` — plus **PriorityClasses**: state (Postgres/NATS) =
  highest, Ollama replicas = low/preemptible. Normal ops: state sits on node-3, LLMs on compute. Lose
  node-3: state reschedules onto a compute node (Longhorn already holds the replicated volume there) and
  **preempts that node's Ollama replica** (scaled to 0 under resource pressure). Heavy enrichment pauses
  to one-or-zero replicas until node-3 returns; **ingest + filter + embed stay up**. That preserves
  the §2 "survives loss of any one node" guarantee instead of breaking it.
- **Failover is COLD and that's accepted (no hot standby).** Postgres restart + WAL replay + Longhorn
  reattach ≈ a **few-minutes RTO** during which reads *and* writes are down. **No data is lost:** thanks
  to the inference/db-writer split (§3.3), inference keeps consuming the firehose and the db-writer just
  **buffers validated output in NATS** (5d retention) until Postgres returns, then drains the backlog.
  We **reject a streaming replica / Patroni hot standby** on purpose: a physical replica would also carry
  the ~8GB HNSW index on an already-loaded 16GB compute node — the wrong RAM trade for a single-user
  ephemeral system. Mitigations instead: tune for fast crash recovery (checkpoint / `max_wal_size`),
  and the dashboard degrades to a "DB failover in progress" state (§6.8) rather than erroring. *Never
  disable Postgres `fsync` to chase throughput — Longhorn durability depends on it.*
- **Keep the 4B resident: `keep_alive: -1`.** The 4B is low duty cycle, so Ollama's default 5-min idle
  unload would force a ~seconds cold reload onto the **user-facing analyst brief**. Pin it resident
  (~3GB, comfortable on 16GB — "RAM comfortable, CPU is the discipline", §2). Interlocks with failover:
  a preempted replica is scaled to **0** (frees its 3GB for Postgres), so resident-keep on the surviving
  node stays in budget.
- **Point Longhorn's storage path and k3s local-path at the NVMe mount explicitly** — never let it
  default to the SD card.

---

## 3. Security architecture

### 3.1 Trust-zone segmentation (blast-radius containment)
Each layer is its own **namespace** with a default-deny `NetworkPolicy`, its own
`ServiceAccount` + least-priv RBAC, and its own **NATS account** (broker-enforced isolation).
The **ingesters parse attacker-controllable internet data → they get the least power.**

```
 NAMESPACE          EGRESS                       IN-CLUSTER ACCESS        PERSISTENT?
 ───────────────────────────────────────────────────────────────────────────────────
 zone-ingest (DMZ)  ONLY its upstream host       NATS publish only        no
                    (per-ingester allow-list)    (account: ingest.*)
 zone-process       NONE (in-cluster Ollama)     ↓ split by SA (§3.3)      ephemeral only
   ├ inference       NONE                         NATS pub/sub — NO DB
   └ db-writer       NONE                         NATS sub + DB RW
 zone-data          NONE (no internet)           reachable only FROM       Postgres, NATS
                                                  zone-process db-writer    state, pgvector
 zone-present       NONE                          DB READ-ONLY role         no
                    (← user reaches this)
```
A compromised ingester can: phone home nowhere except its one upstream; read no DB; reach no
other pod; publish to one NATS subject. Worst case = corrupt one input stream — which the
corroboration layer already distrusts.

### 3.2 Container hardening (every image)
Pod Security Standard `restricted`, plus per-pod `securityContext`:
- `runAsNonRoot`, pinned high UID, `readOnlyRootFilesystem: true` (writable `emptyDir` only
  where required)
- capabilities `drop: ["ALL"]`, `allowPrivilegeEscalation: false`,
  `seccompProfile: RuntimeDefault`
- **Distroless / `scratch`** base — no shell, no package manager in the running image
- CPU/memory `limits` on everything (resource-exhaustion / DoS containment)
- Untrusted input is **schema-validated + size-capped at the NATS boundary** before anything
  downstream touches it (Pydantic models, reject-and-drop on violation)

### 3.3 LLM as a pure function (prompt-injection containment)
Enrichment LLMs read untrusted firehose text, which *will* contain injection attempts.
Structural mitigation: models **classify/extract/summarize only — no tools, no network, no DB
writes.** Their output is schema-validated like any other untrusted input. Worst case from a
successful injection = one bad classification, absorbed by corroboration.

All enrichment LLMs are now **local** — the in-cluster Ollama/Qwen3-4B service plus the
small filter LLM, on the compute nodes — with **no internet egress at all**. A successful injection
therefore cannot exfiltrate or call out: the worst it can do is taint one classification. Model
weights are pulled once at bootstrap (pinned by digest) and never reached again at runtime.

**Inference / DB-writer split (defense in depth).** The "pure function" guard above covers a bad
LLM *output*, not a memory-corruption RCE in a tokenizer / ONNX / llama.cpp runtime — and those pods
parse attacker-controlled text just like the ingesters do. So every worker that *parses untrusted
text* — the firehose classifier, embedder, NER, and the Ollama/Qwen + small LLM — runs under an
**inference ServiceAccount with NO database credentials and no route to zone-data**; it consumes raw
items from NATS and publishes only **schema-validated** structured output back to NATS. A separate
**db-writer** consumer (its own SA, the only thing in zone-process with DB RW) reads that
already-validated output and writes it. A runtime exploit in any inference pod therefore lands
somewhere that can reach neither the database nor the internet — the same least-power posture
zone-ingest already enjoys.

### 3.4 Supply chain
GitHub Actions: `docker buildx` multi-arch (`linux/arm64` + `linux/amd64`) → push **GHCR** →
**Trivy/Grype** scan (fail on criticals) → **SBOM** (syft) → **cosign** sign → manifests
reference **pinned digests**, not tags. Reproducible from a clean `git clone`.
**Model artifacts** (Qwen3-4B GGUF, bge-small ONNX, the filter classifier) are pulled from
source **once**, pinned by digest/checksum, and baked into an image or a Longhorn volume at bootstrap
— never fetched at runtime (keeps the enrichment zone zero-egress, §3.3).

### 3.5 Secrets & zero-trust identity
- **Generate-in-cluster > commit-encrypted.** Postgres passwords, NATS nkeys/account creds,
  internal TLS → generated inside the cluster (operators / init jobs / cert-manager). Never
  exist outside it.
- The only externally-originating secret — the **GHCR pull token** — is **SOPS+age** encrypted and
  committed. (The Anthropic API key is **gone**: enrichment is now local Ollama/Qwen3-4B — zero LLM
  egress, one fewer crown-jewel credential to manage and rotate.)
- **Crown jewels never in the repo (encrypted or not):** the age private key and the Flux
  deploy key. They live in-cluster + your password manager only.
- The enrichment LLM is the in-cluster **Ollama** service (`zone-process`, compute nodes); it holds
  no secret and reaches no network.

### 3.6 Secret hygiene for private→public repo
Because git history is forever, secrets must never enter history at all. Four gates:
1. `.gitignore` (env files, `*.pem`, `*.key`, `*.age`, `kubeconfig`, decrypted manifests)
2. **pre-commit hook**: `gitleaks` + `detect-secrets` block locally
3. **CI gate**: `gitleaks`/`trufflehog` scan every push **and full history**; fail on hit
4. **pre-public audit checklist**: full-history scan; **rotate anything ever committed**; if
   history is dirty, squash to a clean single commit / fresh repo before going public

---

## 4. Data lifecycle — hard 5-day wall

Everything content-bearing is gone at 5 days. No exceptions, no aggregates exemption, no
archival backups.

| Layer | Enforcement |
|---|---|
| NATS JetStream | stream `MaxAge = 5d` (auto-expire) |
| Postgres + pgvector | **daily declarative partitions**; CronJob **`DROP`s** partitions > 5d (never `DELETE` — avoids bloat/VACUUM write-amplification) |
| Embeddings | same partitioned tables → age out with Items |
| Dedup store (NATS KV) | exact + simhash/LSH dedup keys carry a **5d TTL** → can't dedup against >5d content (§6.3 step 2) |
| Logs | retention capped at 5d (logs can carry source content/PII) |
| Longhorn snapshots | retention capped so snapshots can't resurrect >5d data |
| Backups | **none** — in-cluster replication only |

**Resilience trade:** Longhorn 2–3× replication covers node failure. A catastrophic full-cluster
loss = lose ≤5 days of intentionally-disposable data. Consistent with ephemerality. Single-node loss
of the state node is a **cold failover** (few-minutes RTO, reads+writes down) but **lossless** — the
db-writer buffers in NATS until Postgres returns (§2, §3.3). Hot standby rejected on RAM grounds (§2).

**Training-data consequence (filter improvement is bounded by the wall):** a learned filter classifier
needs *text* to train on, and labeled firehose text is content-bearing + PII-bearing — exactly what the
wall deletes at 5 days. So **there is no growing historical training corpus**; the classifier improves
**online over the rolling 5-day window** (weak labels from downstream trust outcomes + a governor
exploration sample, §6.3 step 2 / §6.3a), seeded by a small **curated** set (reference data, like the
gazetteer — not retained firehose, so wall-compatible). Accepted limitation, not a bug.

**Scoring consequence:** velocity baseline is a **rolling 5-day window** → detects
short-horizon acceleration (right for "developing"). First ~5 days after launch = baseline
warming; UI shows a **"baseline warming"** state so early scores aren't over-trusted.

**Capacity consequence:** the 5-day window also fixes the embed working set — social-class survivors
capped at **≤400k/day** → ~2.0M live vectors ≈ ~8GB HNSW index, held automatically by the adaptive
governor (§6.3a, §2). The window stays at **5 days**: breaking + medium-developing stories corroborate
well inside it, so no 7-day extension is needed (slow-burn stories are explicitly out of scope).

---

## 5. System architecture (5 layers)

```
 1. INGEST   per-source hardened pods → normalize → validated Item → NATS ingest.<source>
 2. ENRICH   filter(classify+gazetteer-tally+dedup) → embed → NER/geo → validated → NATS → db-writer
 3. CLUSTER  online clustering → Stories; per-Story claim-extract + provenance graph
 4. SCORE    corroboration gate + gap score + inauthenticity penalty
 5. SURFACE  ranked feed · trust badges · velocity sparklines · map · alerts · read API
```

---

## 6. Application logic

### 6.1 Sources

**Primary / authoritative (high trust — can *promote* a story to PRIMARY_BACKED):**
- **USGS earthquakes** (GeoJSON, poll 1–5 min)
- **NOAA/NWS alerts** (CAP/XML, poll) — severe weather
- **GDACS** disaster alerts (feed)
- ~~ReliefWeb~~ *(dropped 2026-07-03: API now requires an approved appname and RSS is
  bot-blocked — not worth the registration dependency; GDACS covers the disaster signal)*
- **Wikipedia EventStreams** (SSE `recentchange`) — edits, esp. those citing primary docs
- **U.S. gov agency press feeds** (RSS, poll) — official statements, all on the shared
  `services/ingest/press` template. Shipped 2026-07-11: **State** (`state.gov`), **DoD**
  (`war.gov` — the dept rebranded to "Department of War"), **CISA** (cyber advisories),
  **CDC** (newsroom; `max_age_days`-bounded — the feed carries ~1800 items of history).
  WAF gotcha: some `.gov` hosts 200-OK-block non-browser UAs → a `Mozilla/5.0 (compatible; …)`
  UA is the default. **DHS + Treasury deferred**: no usable public RSS (DHS exposes none;
  OFAC retired its feed 2025-01-31) — need an alternative (GovDelivery/Atom) if pursued.
- **OpenSky / ADS-B** flight telemetry *(optional, later)*
- **NASA FIRMS** active-fire *(optional, later)*

**Social / raw (high noise — require corroboration):**
- **Bluesky Jetstream** (WS firehose)
- **Mastodon streaming** (public timeline, selected instances, WS)
- **Nostr relays** *(optional, later)*

**Mainstream baseline (presence only — NEVER content-scraped):**
- **Google News RSS** (topic/entity queries), **AP / Reuters wire RSS**, **GDELT mainstream
  article index**. Used for headline/metadata presence to compute `mainstream_presence`.

> GDELT pulls double duty: a primary-ish event/GKG signal *and* a mainstream-coverage index.

### 6.2 Common Item schema
```
Item {
  id            uuid
  source        str            # "bluesky", "usgs", ...
  source_class  enum           # authoritative | primary | social | mainstream
  ts_observed   timestamptz    # when we saw it          (partition key = date)
  ts_event      timestamptz?   # when it happened, if known
  lang          str
  text          str            # size-capped
  entities      [Entity]       # NER: people/orgs/places + geo
  geo           {lat,lon,geohash}?
  urls          [str]          # canonicalized
  author_ref    str            # HASHED account id (no raw handle stored long-term)
  parent_ref    str?           # quote/repost/reply target → dependency edge
  content_hash  str            # exact + simhash for dedup/independence
  raw_ref       str            # pointer; raw blob also under 5d TTL
}
```

### 6.3 Enrichment — filter-then-embed
Order matters (cheap → expensive) to protect Pi CPU and local-embed throughput:
1. **Boundary validation** — Pydantic schema + size cap; reject-and-drop on violation.
2. **Cheap filter (full firehose, pre-embed)** — language detect; **exact + near-dup
   dedup-to-representative** against a **shared dedup store (NATS KV, 5d TTL — §4)**: exact via
   `content_hash`, near-dup via **simhash + an LSH banding index** (sub-linear, not a pairwise scan).
   Copypasta collapses to one embedded item; independent origins are still counted from metadata
   (§6.5). The store is **shared across classifier replicas** so dedup is correct cluster-wide — an
   in-process LRU (the 0a stub) would miss cross-replica dupes and can't span 2 nodes. Then spam/bot
   heuristics and the **adaptive relevance gate** (admit top-X% by relevance, X set by the §6.3a
   governor). The relevance score "is this a newsworthy factual claim?" comes from a **standalone local
   text classifier** — fine-tuned sub-100MB encoder (MiniLM/DistilBERT) or SetFit head, ONNX on ARM,
   **text-only** (it does *not* read entities/geo — those don't exist yet at this stage) — run on the
   **full firehose**, separate from the embedder (step 3) so classify/embed stay decoupled and the
   governor keeps a true pre-embed signal. Borderline-subset escalation runs a **local small instruction
   LLM** (SmolLM2 / Qwen2.5-0.5B, Apache-2.0, GGUF/llama.cpp) — *not* Haiku, so the whole filter path is
   local, **zero internet egress**, and a pure function per §3.3. Exact models TBD by a Pi spike.
   **Filter warming (cold-start):** day-1 there is no labeled data, so the classifier ships seeded from a
   small **curated** set (SetFit few-shot; curated reference data, wall-compatible) and the gate runs
   **permissively** early (high admission percentile — cheap budget headroom while baselines warm). It
   then improves **online over the rolling 5-day window** (the wall forbids a growing corpus, §4): weak
   labels from downstream trust outcomes (items that reached `CORROBORATED`/`PRIMARY_BACKED` = positive;
   CIB/noise = negative) **plus the §6.3a exploration sample** of the shed tail (counterfactuals — without
   them the loop only relearns its own admissions). The dashboard shows a **"filter warming"** state
   (§6.8) so early filtering isn't over-trusted. The retrain loop lands in Phase 4/5 (§10); 0a/0b ship the
   seeded classifier only.
   **Velocity entity-mentions are tallied here too — closing the §6.6 ordering gap:** a **cheap
   gazetteer/keyword matcher** (Aho-Corasick over a curated people/org/place list, place→geo) runs on
   the **full firehose** to emit **mention tallies + coarse geo** for velocity (§6.6). This is
   deliberately *not* the model-based NER of step 4 (which is post-filter, survivors-only) — velocity
   needs a firehose-wide signal, so it gets a cheap firehose-wide tagger. Tallies are pre-embed and
   unthrottled, so admission control (§6.3a) can't bias them.
3. **Embed survivors** — local quantized model (MiniLM/BGE-small class, ONNX Runtime, CPU). Throughput
   is ample (~1 emb/s at 100k/day vs ~50–90/s per node); the embed stage is gated by index RAM, not CPU.
4. **Extract — per-survivor, CHEAP only** — NER + geo resolution + URL canonicalization over survivors
   (≤400k/day ≈ 4–5/s), **batched**, on a small **ONNX token-classifier** — never a 4B (even
   non-thinking, a 4B on Pi CPU can't hold ~4–5/s, §12). **Claim extraction is deliberately *not* here:**
   isolating the factual assertion is a generative/reasoning task too heavy for the survivor rate even
   on a 0.5B LLM, so it moves to **Story formation (§6.4)** — tens–hundreds/day, riding the `llm.heavy`
   queue. Step 4 produces only the cheap structural signal clustering needs (entities + geo); meaning
   extraction happens once an item has actually joined a Story.

**LLM tiering — now fully local, ordered by *volume* (the binding constraint on Pi CPU):**
- **Full firehose** → standalone local **classifier** (fine-tuned MiniLM/SetFit, ONNX/ARM) for the
  relevance gate; **borderline subset** → local **small instruction LLM** (SmolLM2 / Qwen2.5-0.5B
  class). Both zero-egress, see §6.3 step 2.
- **Per-survivor** (≤400k/day) → **NER + geo only** on a cheap small ONNX token-classifier — never the
  4B, and **claim extraction deferred to Story-level** (§6.3 step 4, §6.4).
- **Per-Story, low-volume** → **Ollama Qwen3-4B** (thinking-capable; 2 HA replicas, compute nodes):
  **claim extraction**, story summarization, ambiguous entity resolution, corroboration reasoning, and
  the **on-demand analyst brief**. **The gate is a concrete queue, not a guideline:** a dedicated NATS
  `llm.heavy` subject that **only these producers** feed — (a) a Story crossing the pre-surface gap
  threshold, (b) the §6.4 consolidation pass flagging an ambiguous cluster, (c) **claim extraction for
  members of *candidate* Stories only** — those that reach a corroboration/velocity threshold (≥2
  independent origins or rising velocity), **never every cluster** (most embedded items join
  low-relevance stories that never develop, so gating here keeps claim-extract off the 400k stream),
  (d) an explicit analyst-brief request. Nothing else ever reaches the 4B; consumer concurrency = **2**
  (one per replica), so load is bounded by construction (tens–hundreds of stories/day, not the survivor
  stream). Corroboration counting (§6.5) and primary-match run on the **cheap NER+geo** from step 4 —
  they don't need deep claim extraction, so the gate doesn't starve trust scoring.
- **4B mode policy — DECIDED 2026-06-28:** one model, **run non-thinking by default**
  (`enable_thinking=false` / `/no_think`) for summarize / consolidate / entity-resolution — fast, no
  long CoT. **Thinking is enabled only for the on-demand analyst brief** (top-ranked, user-triggered,
  latency-tolerant). Per-request flag on the same Ollama model — no second deployment.
- Embeddings via **bge-small** (384-dim, matches `vector(384)` in schema), never an LLM.

The three former Anthropic tiers (Haiku/Sonnet/Opus) collapse into this one local 4B; differentiation
is now by **task + queue priority + replica count + thinking flag**, not model size — zero egress.

### 6.3a Adaptive admission control — embed-budget governor
A closed-loop controller holds the social-class survivor rate at the RAM-derived budget (≤400k/day,
§2) with **no operator intervention**. It governs only the social firehose; **authoritative/primary
sources bypass it entirely.**

- **Controlled variable:** survivor rate (EWMA over ~5–15 min).
- **Actuator:** the §6.3 relevance gate expressed as an **admission percentile** ("embed top-X%"), not
  an absolute threshold — robust to score drift and volume swings; always sheds the least-relevant tail.
- **Control law:** integral / AIMD with a **deadband** (ignore noise) + **slew limit** (no oscillation).
  Under budget → X relaxes toward a floor (never waste capacity); over budget → X tightens.
- **Burst vs. sustained:** the loop reacts only to *sustained* error. Short spikes (real breaking
  events) are absorbed by the NATS JetStream buffer and pass through unshed. If buffer depth crosses a
  high-water mark it clamps harder **and auto-raises the "sampling active" flag** so downstream scoring
  self-discounts — no human sets it.
- **Ceiling regime:** sustained over-budget = the "needs another node" case software can't fix by
  conjuring RAM. Behavior is **graceful degradation, not failure**: hold at budget by shedding the
  lowest-relevance tail, keep every authoritative item, keep running, emit one annotation
  ("at embed ceiling Nh+, consider 4th node"). See §12.
- **Robustness:** budget is a **config value derived from RAM / window / node-count** (capacity changes
  just update one number and the loop re-converges); control state (X, EWMAs) persists in **NATS KV** so
  a pod restart / Longhorn failover resumes mid-stream instead of re-converging cold.
- **Exploration quota (for filter training, §6.3 step 2):** the gate **always embeds a small random
  sample (~1–2%) from *below* the admission threshold**, tagged `exploration`. These shed-tail
  counterfactuals are the negative/uncertain labels the classifier retrain loop needs — without them
  weak-labeling only sees admitted items and the filter collapses to relearning its own decisions. The
  sample is tiny relative to the budget and exempt from the velocity tally (it's not a real mention
  signal). Off during the ceiling regime (no spare budget to explore with).

### 6.4 Clustering — online
- For each new embedded Item: ANN query over **pgvector (HNSW)** for near neighbors within
  (cosine ≥ θ) ∧ (time window) ∧ (shared entity). Assign to existing **Story** or open a new one.
- **This ANN query — not the embed — is the per-item hot path.** It runs once per embedded survivor
  (≤400k/day) and on Pi CPU likely **dominates** the embed cost. Two constraints make it real: (i)
  pgvector **HNSW indexes are per-partition**, so with daily partitions a naive query fans out across
  all 5 daily indexes and merges; (ii) HNSW search is CPU-bound. **Mitigation:** clustering only cares
  about *near-in-time* neighbors, so **restrict the ANN to the current + previous day partition(s)**
  (1–2 indexes, not 5) — the time-window predicate already implies this; make it an explicit partition
  prune. Tune `hnsw.ef_search` for the recall/latency trade. **Phase-0b must measure this query's
  latency** (added to §10 0b), not just embed throughput.
- Story keeps: centroid, entity set, member Items, source set, provenance edges, first/last seen.
- Periodic **consolidation pass** (local Qwen3-4B, non-thinking, on ambiguous clusters only) merges/splits.
- Everything inside the 5-day window; Stories age out with their Items.

### 6.5 Corroboration & provenance — the differentiator
**Provenance graph per Story.** Add a *dependency edge* between two Items when any of:
- (a) one references/quotes/reposts/replies to the other (`parent_ref`)
- (b) simhash distance below threshold (copypasta) — resolved via the **LSH banding index** from §6.3
  step 2, not a pairwise scan, so this stays sub-linear at firehose scale
- (c) same canonical upstream URL
- (d) same `author_ref`, or same coordinated author-cluster (see 6.7)

**Independent origins** = count of weakly-connected components in the dependency graph, further
deduped by distinct org/domain. We count *independent origins*, **not raw item count** — this
defeats circular reporting and single-source amplification.

**Trust states:**
| State | Rule |
|---|---|
| `RUMOR` | 1 independent origin |
| `CORROBORATED` | ≥ **N=3** independent origins (configurable) **spanning ≥ 2 distinct sources** |
| `PRIMARY_BACKED` | a social claim matched to an authoritative/primary record |

The **≥2-distinct-sources floor** on `CORROBORATED` (computed at the origin level, so
wire-syndication and same-org collapse first) blocks single-platform amplification: N
distinct Bluesky accounts alone stay `RUMOR`. It does **not** blunt the gap mission —
cross-platform social (Bluesky+Mastodon) or social+local still corroborate *before*
mainstream. `PRIMARY_BACKED` is inherently ≥2 sources (social ∧ primary) so it is
unaffected. Source count is configurable (`MIN_CORROBORATION_SOURCES=2`).

**Primary-match** = entity ∧ geo ∧ time alignment between a social claim and a primary record
(e.g., social "big quake in X" + USGS event, same region/time → promote). `PRIMARY_BACKED` can
promote with few sources because the primary record *is* the evidence.

Only `CORROBORATED` / `PRIMARY_BACKED` are alert-eligible. `RUMOR` is visible but badged and
never alerted.

### 6.6 Gap score
```
gap = velocity_z × (1 − mainstream_presence) × corroboration_weight × (1 − inauthenticity)
```
- `velocity_z` — z-score of mention **acceleration** (EWMA + 2nd derivative) for the Story's
  entity set over the rolling 5-day baseline. **Counted from the pre-embed firehose entity-mention
  tallies of the §6.3 step 2 gazetteer matcher** (a firehose-wide signal, *not* the post-filter
  survivor-only NER of step 4), **never the embedded/throttled count — so adaptive admission control
  (§6.3a) cannot bias it.**
- `mainstream_presence` ∈ [0,1] — normalized matched coverage in the mainstream baseline. Low =
  underreported.
- `corroboration_weight` — RUMOR 0 · CORROBORATED 0.7 · PRIMARY_BACKED 1.0.
- `inauthenticity` ∈ [0,1] — coordinated-behavior penalty (6.7).

High gap = corroborated + accelerating fast + mainstream hasn't caught up = your target signal.
**Alert** when `gap > threshold` ∧ `trust_state ≥ CORROBORATED`.

### 6.7 Coordinated-inauthenticity & source reputation
Per-Story `inauthenticity` from: burst from low-age/low-reputation accounts; synchronized
posting times; low author diversity (few origins, massive amplification); identical text across
many accounts (copypasta network). **Note:** reputation is computed **within the 5-day window**
only (hard wall — no long-term reputation store). Short-horizon, but sufficient for burst/CIB
detection.

### 6.8 Surface — dashboard, API, alerts
- **Read API:** FastAPI in `zone-present`, **read-only** DB role.
- **Dashboard:** ranked Story feed — gap score, **trust badge** (RUMOR/CORROBORATED/
  PRIMARY_BACKED), velocity sparkline, source breakdown, mainstream-presence indicator,
  "baseline warming" flag, **map view** for geo events.
- **System-health states:** **"filter warming"** (classifier still cold-starting, §6.3 step 2 — early
  filtering not fully trusted), the §6.3a **"sampling active"** flag, and a **"DB failover in progress"**
  banner that degrades gracefully (read-only/last-known feed, no hard error) during a cold state-node
  failover (§2, §4) instead of erroring out.
- **Alerts:** push on `gap > threshold` ∧ corroborated. Single-user → keep egress minimal;
  prefer **self-hosted ntfy in-cluster** (no third-party egress) over email/webhook.

---

## 7. Tech stack

- **Python 3.12**, `asyncio`; `httpx` + `websockets` (Jetstream/Mastodon WS, Wikipedia SSE);
  `pydantic` (boundary validation); polling for GDELT/USGS.
- **NATS JetStream** (`nats.py`) — bus, per-account isolation, 5d MaxAge; **NATS KV** also backs the
  shared dedup store (content_hash + simhash/LSH, 5d TTL) and the §6.3a governor state.
- **Postgres 16 + pgvector** (`asyncpg`/SQLAlchemy) — daily partitions, **per-partition HNSW** (queries
  prune to the recent 1–2 partitions, §6.4).
- **ONNX Runtime + sentence-transformers** — local **bge-small** embeddings (384-dim), the text-only
  filter classifier, and the per-survivor NER token-classifier.
- **Gazetteer matcher** (Aho-Corasick, e.g. `pyahocorasick`/`flashtext`) — cheap firehose entity/geo
  tally for velocity (§6.6).
- Enrichment splits into **inference workers** (parse untrusted text, no DB creds) and a **db-writer**
  (validated output → Postgres) — §3.3.
- **Ollama serving Qwen3-4B** (Apache-2.0, thinking-capable, run non-thinking by default; 2 replicas, HA + burst, compute nodes) — local
  enrichment via its in-cluster OpenAI-compatible API; **zero internet egress**, no Anthropic SDK/key.
- **llama.cpp / GGUF small LLM** (SmolLM2 / Qwen2.5-0.5B) — borderline-filter escalation.
- **Prometheus + Grafana** — metrics + dashboards on the state node.
- **FastAPI** + lightweight dashboard.
- **k3s · Longhorn · Flux · SOPS/age · cosign · syft · Trivy/Grype · gitleaks · cert-manager.**
- **Distroless** base images, multi-arch.

---

## 8. Repository layout
```
news_aggregator/
  PLAN.md
  README.md
  .gitignore                 # secrets, keys, kubeconfig, *.age, decrypted manifests
  .pre-commit-config.yaml    # gitleaks + detect-secrets
  .github/workflows/         # build · scan · sbom · sign · secret-scan(full history)
  services/
    ingest/                  # one hardened image per source (shared base)
    enrich/                  # inference workers: filter+classify+gazetteer, embed, NER+geo (NO DB)
    writer/                  # db-writer: validated NATS output -> Postgres (only DB-RW pod, §3.3)
    cluster/                 # online clustering (ANN→Story, llm.heavy producer) + claimx (4B claim-extract, NO DB)
    score/
    api/
    dashboard/
  libs/
    schema/                  # Item/Story Pydantic + DB models
    bus/                     # NATS helpers (account-scoped) + KV dedup store
    embed/                   # ONNX embedder (bge-small) — hash fallback in 0a
    dedup/                   # content_hash + simhash/LSH over NATS KV (5d TTL)
    gazetteer/               # cheap firehose entity/geo tally for velocity (§6.6)
    classify/                # text-only relevance classifier (heuristic fallback)
    ner/                     # per-survivor NER+geo (no-op fallback in 0a)
    llm/                     # local Ollama client — Qwen3-4B (pure-function guardrails)
  deploy/
    helm/                    # the chart (values: dev / pi-cluster)
    flux/                    # GitOps sources, kustomizations, image automation
    policies/                # NetworkPolicies, PSS, RBAC, NATS accounts
    secrets/                 # SOPS-encrypted ONLY (age pubkey in .sops.yaml)
  bootstrap/                 # k3s + NVMe + Flux + SOPS runbook & scripts
  docs/
```

---

## 9. GitOps & CI/CD
- **Flux** pulls the private repo (read-only deploy key, in-cluster only) and reconciles
  `deploy/`. Native **SOPS** decryption via an in-cluster age key.
- **CI** (GitHub Actions): lint/test → buildx multi-arch → Trivy/Grype → SBOM → cosign sign →
  push GHCR (pinned digests) → secret-scan (full history) gate.
- Image automation (Flux) optional later; manual digest bumps acceptable for single-user.

---

## 10. Phased delivery

| Phase | Deliverable |
|---|---|
| **0a — Secure skeleton (dev box, no hardware)** | All code + manifests + CI, validated on **k3d in CI** (`k3d-e2e.yml`: GitOps flow, PSS + netpol enforcement, SOPS round-trip). The dev box is **zero-trust (rootless docker, no root)** and cannot host k8s — its parity target is docker-compose only. 4 namespaces, default-deny NetworkPolicies, NATS accounts, Postgres(partitioned)+pgvector, 5d-TTL CronJob, Helm chart, Flux reconciliation, SOPS decryption, signed multi-arch CI (buildx+QEMU for arm64), secret-scanning, **one hardened USGS ingester** end-to-end. Proves trust-zone isolation + GitOps + secret pipeline *without a single Pi*. Parity targets: `docker-compose` (fast inner loop, dev box) + `k3d` (k8s fidelity, CI). |
| **0b — Cluster bootstrap (Pi hardware)** | Runbook: [`docs/0b-bootstrap-runbook.md`](docs/0b-bootstrap-runbook.md). Bootstrap end-to-end: Pironman 5 assembly + **pinned `pironman5` daemon** (fan curves, safe shutdown, RGB off, no auto-update, verify zero egress) + force PCIe Gen3, NVMe boot, k3s 3-server etcd HA across the 3 physical nodes, Longhorn replication/failover, Flux + SOPS bootstrap. Deploy the **already-proven** 0a manifests. *De-risked in software 2026-07-07 (PR #32): the topology mechanics — `role` placement, PriorityClass preemption (na-state evicts low-pri filler), compute-node drain-failover with the pipeline still flowing — are asserted on a 3-node k3d by `k3d-multinode-e2e.yml`, and arm64 wheel health (onnxruntime/onnx/asyncpg/numpy import on aarch64) by `arm64-smoke.yml` under QEMU. What follows is the genuinely hardware-gated remainder.* Real-world validation only available here: **filter-then-embed throughput on actual Pi CPU**, **clustering ANN-query latency over partitioned HNSW** (the per-item hot path, §6.4 — measure this, not just embed), **firehose gazetteer-tally + dedup/LSH throughput**, thermal/power/NVMe-under-load, Longhorn multi-node failover. Stand up **Ollama (Qwen3-4B ×2)** with `role=compute`/`role=state` labels, podAntiAffinity, PriorityClasses (state preempts an LLM replica on node-3 loss, §2), pull pinned model weights, and **Prometheus/Grafana**. The gating spike here is **4B per-item latency on Pi CPU** — it confirms the Story-volume headroom the design assumes (§12). → `git clone && flux bootstrap` brings up the locked-down skeleton on real hardware, ingesting one safe (primary) source. |
| **1 — Ingest breadth** | Remaining hardened ingesters (Bluesky, Wikipedia, GDELT, Mastodon), each with its own egress allow-list; all normalized to Item. |
| **2 — Enrich + cluster** | Filter (classify + gazetteer-tally + KV/LSH dedup) → embed → cheap per-survivor NER+geo → inference/db-writer split; online clustering into Stories (partition-pruned ANN); per-Story claim-extract on candidate Stories. |
| **3 — Trust** | Provenance graph, independence detection, corroboration gate, primary-record matching. |
| **4 — Score + surface** | Velocity + mainstream baseline + gap score; ranked dashboard + map + alerts; "filter/baseline warming" + "DB failover" UI states; wire the §6.3a **exploration quota** + weak-label capture (trust outcomes) so training data starts flowing. |
| **5 — Harden signal** | Coordinated-inauthenticity detection, 5d source reputation, **online filter-retrain loop** (rolling-window weak labels + exploration sample → classifier; same machinery as the eval harness), eval harness, policy audit, pre-public secret audit. |

---

## 12. Known risks
- **Cold-start baselines** (first 5 days) — mitigated by "baseline warming" UI state.
- **Filter cold-start ("filter warming") — DESIGNED-OUT.** No labeled data day-1, and the 5-day wall
  forbids a growing training corpus. Mitigated: curated-seed SetFit + permissive early gate, then online
  rolling-window improvement (weak labels + §6.3a exploration quota), surfaced as a "filter warming" UI
  state (§6.3 step 2, §4, §6.8). *Residual:* the rolling-window cap on training data is an accepted limit.
- **State-node DB failover is cold — ACCEPTED.** Few-minute RTO with reads+writes down, but **lossless**
  (NATS buffer, §2/§4). Hot standby deliberately rejected (RAM). *Residual:* recovery-time tuning in 0b.
- **Local-embed throughput** on Pi CPU — mitigated by filter-then-embed; watch as a Phase-2 metric.
- **Heavy-tier LLM throughput on Pi CPU (local pivot — DESIGNED-OUT).** Qwen3-4B on a Pi 5 CPU runs at
  only a few tok/s (and a thinking trace adds hundreds–thousands of tokens → tens of s–min/item).
  **Mitigated by construction:** the 4B is confined to **Story-level, low-volume** work via the bounded
  `llm.heavy` queue (§6.3) and run **non-thinking by default** (§6.3 mode policy); per-survivor work
  (≤400k/day ≈ 4–5/s) is **NER+geo only** on cheap ONNX, with **claim extraction deferred to candidate
  Stories** (§6.3 step 4, §6.4). **Refinement (2026-07-09 benchmark, `docs/pi-throughput-findings.md`):**
  "non-thinking by default" proved **insufficient for the 4B on claim extraction** — it reasons INTO
  content regardless → so **claim extraction runs a small non-reasoning model (qwen3:1.7b, ~4 s/claim
  Pi est), not the 4B**. *Residual:* Phase-0b measures actual per-item latency to confirm
  Story-volume headroom; if even that saturates, add a 4th node.
- **Firehose-classifier CPU contention during storms (DESIGNED-OUT).** The classifier runs on the whole
  firehose and is the binding CPU constraint in a storm. **Mitigated:** it's homed on both compute nodes
  with a guaranteed CPU `request`; Ollama (low duty cycle) yields to it via low-request/high-limit
  (§2 mechanics); extreme storms shed via the §6.3a JetStream buffer + high-water sampling. *Residual:*
  tune the request floor + sampling high-water against real volume in Phase 0b.
- **Embed-budget ceiling** (sustained social-class volume > 400k/day) — RAM can't be conjured, so the
  adaptive governor (§6.3a) **auto-degrades**: holds at budget by shedding lowest-relevance social
  items, keeps all authoritative/primary, raises the "sampling active" flag, and emits a single
  "consider 4th node" annotation. No manual threshold tuning; the only residual action is provisioning.
- **5-day wall vs. baseline quality** — accepted; short-horizon by design.
- **Catastrophic cluster loss** = lose ≤5 days — accepted (data is disposable).
- **Mainstream-presence accuracy** — RSS/GDELT index may lag or miss; tune matching in Phase 4.
