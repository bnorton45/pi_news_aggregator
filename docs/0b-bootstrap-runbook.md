# Phase 0b — Cluster bootstrap runbook (Pi hardware)

Turns 3 bare Raspberry Pi 5s into the locked-down cluster and deploys the **already-proven**
0a manifests (PLAN §10). Everything below the Flux bootstrap is the genuinely hardware-gated
remainder — the topology *mechanics* (role placement, PriorityClass preemption, compute-node
drain-failover) are already asserted in CI by `k3d-multinode-e2e.yml`, and arm64 wheel health by
`arm64-smoke.yml`. This runbook is the on-hardware procedure that CI cannot cover.

**Design invariants that must survive bootstrap** (never weaken to make a step pass — PLAN §3): zero-trust / zero runtime egress, default-deny NetworkPolicies, PSS restricted, SOPS
scoping, the age private key + Flux deploy key never entering the repo. Widening any of these needs
explicit sign-off.

> Legend: ✅ = covered by committed manifests · ⚠️ = **manifest gap, author before/at 0b** (Ollama,
> Prometheus/Grafana are named 0b deliverables not yet in the chart) · 🔬 = a 0b measurement spike.

---

## 0. Before you start — bring these

**Bill of materials** (PLAN §2, LOCKED):
- 3× Raspberry Pi 5, 16 GB
- 3× SunFounder Pironman 5 **standard** (NOT MAX — the MAX's ASM1182e switch downgrades PCIe to Gen2)
- 3× Samsung 990 EVO 1 TB NVMe (M.2, into the Pironman slot)
- 3× 5V/5A USB-C PD supply; UPS recommended; wired Ethernet + switch; 3× SD card (recovery only)
- **No Hailo-8L** (slot is the SSD's; nothing in this design runs on it)

**Credentials / artifacts to have in hand** (none of these live in git):
- **GHCR PAT** with `read:packages`, for user `bnorton45` → used as the pull-secret password
- Ability to generate an **age keypair** (`age-keygen`) → private half goes in-cluster + your password
  manager, public half replaces the placeholder recipient in `/.sops.yaml`
- A **Flux deploy key** for the private repo (read-only) — `flux bootstrap` can create it via a GitHub
  PAT with `repo` scope, or supply your own
- Workstation tooling: `kubectl`, `helm`, `flux` CLI, `sops`, `age`, `ssh`

**Pin everything at provision time** (supply-chain, PLAN §3.4): k3s version, Longhorn version, the
`pironman5` daemon version, Ollama image + model digests. No auto-update on any node.

---

## 1. Per-node hardware assembly

For each of the 3 units:
1. Seat the NVMe in the Pironman 5 M.2 slot (direct to the Pi's PCIe FFC — single slot, that's why
   standard not MAX). Tower cooler + dual fans mounted (active cooling is required; NVMe + Pi 5 under
   sustained DB load runs hot).
2. Assemble the enclosure; connect Ethernet and the 5V/5A PD supply.
3. Note each unit's MAC for DHCP reservations — you want stable IPs for the 3 k3s servers.

---

## 2. OS flash + base config (each node)

1. **Always flash a clean OS — never a vendor image.** Ubuntu Server 24.04 arm64 **or** RPi OS Lite
   64-bit (Bookworm).
2. **Force PCIe Gen3** and enable memory cgroups. On RPi OS, in `config.txt` / `cmdline.txt`:
   - `config.txt`: `dtparam=pciex1_gen=3`
   - `cmdline.txt`: append `cgroup_memory=1 cgroup_enable=memory`
3. **Boot from NVMe** (set the boot order via `raspi-config` / `rpi-eeprom-config`); SD card is
   recovery-only. Verify root is on the NVMe (`findmnt /`), not the SD card.
4. Static IP / DHCP reservation, hostname (`node-1`/`node-2`/`node-3`), SSH key auth only, unattended
   OS security updates off (pin the box; you patch deliberately).
5. Sanity: `lspci` shows the NVMe link at **8 GT/s** (Gen3), not 5 GT/s (Gen2).

---

## 3. `pironman5` daemon — pinned, no egress (each node)

Fans / OLED / safe-shutdown need SunFounder's `pironman5` daemon: a **third-party root systemd service
touching I2C/GPIO** (PLAN §2, and a tracked 0b supply-chain item).
1. Install a **pinned** version (record the exact commit/tag); **disable auto-update**.
2. Config: **RGB off** (headless — power/noise), fan curve set, safe-shutdown button enabled.
3. **Verify zero egress**: confirm the daemon opens no outbound sockets (`ss -tunp`, or a brief
   `tcpdump` while idle). It must not phone home. This is a security-posture gate, not a nicety.
4. Point any of its writable state at the NVMe, not the SD card.

---

## 4. k3s — 3-server HA with embedded etcd

Survives loss of any one node (PLAN §2). etcd on NVMe handles the fsync load.

1. **Put k3s + Longhorn data on the NVMe explicitly** — never let them default to the SD card.
   Set `--data-dir` to an NVMe path (e.g. `/mnt/nvme/rancher/k3s`).
2. **node-1 (init the cluster):**
   ```
   curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=<pinned> sh -s - server \
     --cluster-init --data-dir /mnt/nvme/rancher/k3s
   ```
   k3s **bundles traefik by default** — keep it (ntfy + the web dashboard are LAN-only behind it);
   do **not** pass `--disable traefik`. Netpol default-deny is enforced by the CNI + our policies
   (§9) independently of the ingress controller.
3. **node-2, node-3 (join as servers):**
   ```
   curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=<pinned> sh -s - server \
     --server https://<node-1-ip>:6443 --token <node-token> \
     --data-dir /mnt/nvme/rancher/k3s
   ```
   Token: `/var/lib/rancher/k3s/server/node-token` on node-1.
4. Pull `/etc/rancher/k3s/k3s.yaml` to your workstation, rewrite the server IP, `export KUBECONFIG`.
5. Verify: `kubectl get nodes` → 3× `Ready`, all `control-plane,etcd,master`.

---

## 5. Label the nodes by role ✅-consumed-by-chart

The chart's `na.nodeAffinity` helper matches a plain **`role`** label (`_helpers.tpl`); unlabeled
nodes leave placement-sensitive pods `Pending`.
```
kubectl label node node-1 role=compute
kubectl label node node-2 role=compute
kubectl label node node-3 role=state
```
Placement (PLAN §2): compute = Ollama replicas + firehose-classify + embedder + ingesters; state =
Postgres+pgvector, NATS JetStream, monitoring, process/cluster/score/API, Flux. Affinity is **soft**
(`preferred`), so a node loss can reschedule state onto a compute node and preempt an Ollama replica —
that's the failover guarantee, don't harden it to a `nodeSelector`.

---

## 6. Longhorn — replicated storage on the NVMe

The chart's `storage.className` is **`longhorn`** (`values.yaml`); Postgres/NATS PVCs bind to it.
Longhorn itself is **not** in the repo — install it out-of-band, pinned.
1. Prereqs on each node: `open-iscsi` installed + `iscsid` enabled; `nfs-common` (RWX).
2. Install a **pinned** Longhorn (Helm or the release manifest). In its settings, **set the default
   data path to the NVMe mount** (e.g. `/mnt/nvme/longhorn`) — never the SD card (PLAN §2).
3. Replica count **2** (matches the ~1.5 TB usable / low-wear intent). Confirm `longhorn` is the
   default StorageClass (or leave the chart's explicit `longhorn` reference to bind it).
4. Verify: `kubectl -n longhorn-system get pods` healthy; a test PVC binds and its replicas land on
   ≥2 distinct nodes.

> Gotcha (from `deploy/flux/apps/helmrelease.yaml`): the HelmRelease renders with **prod (Longhorn)
> values by default** — which is exactly what you want here. The "PVCs pin Pending" warning in that
> file is about the *CI e2e* accidentally getting prod values; on real hardware Longhorn is correct.

---

## 7. Secrets bootstrap — age + the one external secret

Full procedure lives in **`deploy/secrets/README.md`**; summary here. The only externally-originating
secret is the **GHCR pull token**; Postgres + NATS passwords **generate in-cluster** via the chart's
lookup-based generate-or-preserve (nothing to author).

1. Generate the age key (once). Private half → cluster + password manager, **never** the repo:
   ```
   age-keygen -o age.key
   kubectl -n flux-system create secret generic sops-age --from-file=age.agekey=age.key
   ```
2. Put the **public** key in `/.sops.yaml`, replacing the placeholder recipient
   (`age1qqqq…placeh`). Commit that (public key is not a secret).
3. Create + encrypt the GHCR pull secret for both image-pulling zones (see the secrets README for the
   exact `kubectl create secret docker-registry … | sops --encrypt` sequence) → commit
   `deploy/secrets/ghcr-pull.sops.yaml` (ciphertext only; gitleaks/CI gate plaintext).

⚠️ **NATS password gotcha (latent bug, memory / PR #33):** generated NATS passwords **must start with
an alphabetic char** — nats-server re-parses `$NATS_PASS_*` as config tokens and a float-looking draw
crashloops the broker ~1-in-6 per user per fresh install. Confirm the chart's generator still enforces
the alphabetic-prefix constraint before first boot.

---

## 8. Flux bootstrap — GitOps takes over ✅

Command is in **`deploy/flux/README.md`**:
```
flux bootstrap github --owner=bnorton45 --repository=news_aggregator --path=deploy/flux
```
This installs the controllers (read-only deploy key, in-cluster only) and reconciles `deploy/flux` in
dependency order:

| Kustomization | Path | Notes |
|---|---|---|
| `policies` | `./deploy/policies` | namespaces + PSS + netpol + RBAC; `wait: true` (first) |
| `secrets`  | `./deploy/secrets`  | **`suspend: true`** until §7 exists — see below |
| `apps`     | `./deploy/flux/apps`| HelmRelease `news-aggregator`, `dependsOn: policies` |

The `secrets` Kustomization ships **suspended** and decrypts via the `sops-age` secret. Once §7's
`sops-age` + `ghcr-pull.sops.yaml` are in place:
```
flux resume kustomization secrets
```

---

## 9. Verify the locked-down skeleton ✅

- `flux get kustomizations` → `policies`, `secrets`, `apps` all `Ready=True`.
- Namespaces exist: `zone-ingest`, `zone-process`, `zone-data`, `zone-present`.
- **PSS restricted** enforced (namespace labels); **default-deny NetworkPolicies** present in each
  zone (`deploy/policies/netpol-zone-*.yaml`).
- Pods healthy: Postgres + NATS on **node-3** (state); ingesters + enrich on compute; API in
  zone-present. `kubectl get pods -A` — nothing `Pending`/`CrashLoopBackOff`.
- **Images resolve by digest** (PLAN §3.4). Confirm the pinned digests in `values.yaml` are
  **multi-arch manifests including linux/arm64** before you're standing at the Pis — a digest that
  only carries amd64 will `ImagePullBackOff` on the Pis. (CI builds `linux/amd64,linux/arm64`; verify
  with `docker manifest inspect <ref>@<digest>`.)
- **Enrich runs the REAL encoders, not stubs** ✅ — the int8 bge-small + NER are baked into the enrich
  image at `/opt/models/{bge-small,ner}` (Dockerfile `BAKE_MODELS=1`), wired via
  `enrich.embedModelPath`/`nerModelPath`. Confirm in the enrich pod log: `loading ONNX embedder from
  /opt/models/bge-small` (not `using HashEmbedder`). Real vectors are what let stories corroborate
  organically — the stub embedder cannot.

At this point `git clone && flux bootstrap` has brought up the locked skeleton ingesting the safe
(primary) sources. The remainder is genuinely hardware-gated.

---

## 10. Ollama LLM host — deploy ✅, seed weights (operator step)

The Ollama StatefulSet is now in the chart (`templates/ollama.yaml`, `ollama.enabled=true` in prod):
2 replicas in `zone-process`, `app: ollama`, `podAntiAffinity` (one-per-compute-node),
`nodeAffinity: preferred role=compute`, `priorityClassName: na-llm` (lowest — state preempts it on
node-3 loss), low-request/high-limit CPU (yields to the firehose classifier), `keepAlive:-1`, nonroot
+ read-only rootfs, and a per-replica Longhorn PVC at `/models`. The `app: ollama` netpol is
**ingress-only — Ollama reaches nothing** (zero egress by design), so weights are **never pulled at
runtime**; you seed them once, here.

**The one wired client is `claimx` → `qwen3:1.7b`** (the extractive claim task). The 4B was rejected
there — it reasons INTO content and emits 40–280 s of garbage/claim (`docs/pi-throughput-findings.md`,
decided 2026-07-09). A larger model (Qwen3-4B) is **design-retained only** for the future
summarize / entity-resolution / thinking-brief tiers, which have **no service yet** — leave it out
until that tier lands (then add it to `ollama.extraModels` + bump `maxLoadedModels`/memory).

**Seed each replica's PVC** (`models-ollama-0`, `models-ollama-1`) during this provisioning window —
the only moment weights touch the network. Because the pod itself has no egress, do the pull from a
context that does, e.g. **outside default-deny** (pull on the node's containerd / a workstation and
copy the blobs onto the PVC), then let the StatefulSet mount the pre-populated volume. Do **not** add
an egress NetworkPolicy for Ollama to self-pull — that widens posture and needs sign-off.
Pin the model **digest** you seed. Verify: `kubectl exec ollama-0 -- ollama list` shows `qwen3:1.7b`,
and a claim flows llm.heavy → claimx → `claim.*` → a `PRIMARY_BACKED` promotion.

---

## 11. Observability (Prometheus + Grafana) — deployed ✅

In the chart now (`templates/prometheus.yaml`, `grafana.yaml`, `monitoring.enabled=true`), both in
zone-data on the **state** node, fenced by the pre-authored netpol. Prometheus is the **one** workload
with a kube-API token — it uses in-cluster service discovery (a scoped ClusterRole) to scrape
node/kubelet + cAdvisor via the API-server proxy (the `:6443` egress the netpol allows) and any pod
that opts in with `prometheus.io/scrape="true"`. Grafana is LAN-only behind traefik at
`monitoring.grafana.host` (default `grafana.local`), with the Prometheus datasource provisioned and
telemetry/update-checks disabled (zero egress); admin password auto-generated into the `grafana-admin`
secret (`kubectl -n zone-data get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d`).

Operator steps at 0b:
- Add a **LAN DNS / hosts entry** for `grafana.local` → the cluster ingress IP.
- Confirm your **node IPs fall in the `prometheus-egress` ipBlock** (`10.0.0.0/8` or `192.168.0.0/16`)
  so the API-server-proxy scrape is permitted; widen that ipBlock in `netpol-zone-data.yaml` if your
  LAN differs (a netpol edit — expected, not a posture weakening).
- **Thermal / Pi CPU temp / NVMe SMART** aren't in kubelet/cAdvisor — add a `node-exporter`
  DaemonSet in a **separate monitoring namespace** (it needs hostPath + hostNetwork, which the
  PSS-restricted zones forbid), or read the `pironman5` daemon's sensors. Left out of the app chart
  by design; wire it before the §13 thermal spike.
- Build starter dashboards; app-level metrics light up once services expose a named `metrics` port
  (none do yet — the `kubernetes-pods` scrape job is already waiting for them).

---

## 12. End-to-end smoke

- Watch a primary item flow ingest → enrich (real vectors) → cluster → a Story, and surface in the
  API's gap/corroborated/primary/weather tabs (web dashboard, LAN-only behind traefik — confirmed
  form factor).
- Trip an alert: a `CORROBORATED`+high-gap story should publish to **ntfy** (in-cluster, LAN-only);
  subscribe from a phone on the home network.
- Confirm **zero unexpected egress** from the process/data zones (netpol + a spot `tcpdump`).

---

## 13. 🔬 0b measurement spikes — the real reason for hardware

These can only be measured here; feed results back into the open items (PLAN §12):
- **Filter-then-embed throughput** on real Pi CPU (bge-small int8) — the Phase-2 metric.
- **Clustering ANN-query latency** over partitioned HNSW — the per-item hot path (§6.4). Measure
  *this*, not just embed. Tune `ef_search` + partition prune. Gates the deferred local-news ingester.
- **Firehose gazetteer-tally + dedup/LSH throughput** at real volume; tune the §6.3a request floor +
  sampling high-water.
- **4B per-item latency** on Pi CPU — confirms the Story-volume headroom the design assumes (§12); if
  it saturates, that's the "add a 4th node" signal.
- **Confirm `qwen3:1.7b`** (or a dedicated instruct model) for claim extraction on real ARM.
- **int8 NER spot-check** on the real ARM firehose (fp32 is one-line restorable from `onnx/model.onnx`
  if borderline noise bites).
- **Thermal / power / NVMe-under-load**; **Longhorn multi-node failover** RTO.

---

## 14. Failover drills (validate the §2 guarantees on metal)

- **Lose a compute node:** one Ollama replica gone, the other keeps serving; ingest/filter/embed stay
  up; Longhorn replicas still ≥1 elsewhere.
- **Lose node-3 (state):** Postgres/NATS reschedule onto a compute node (Longhorn reattaches the
  replicated volume), **preempting** that node's Ollama replica. Expect a **cold, few-minute RTO** with
  reads+writes down but **no data loss** — the db-writer buffers validated output in NATS (5 d) and
  drains on recovery. The dashboard should show the "DB failover in progress" state, not error.
  **Never disable Postgres `fsync`** to chase recovery speed — Longhorn durability depends on it.

---

## Open items this runbook depends on (track to closure)

- ✅ **Ollama StatefulSet** in the chart (`templates/ollama.yaml`) — deployed; only the **weight
  seeding** (§10) is an operator step, and the summarize/entity-res/brief 4B tier stays deferred.
- ✅ **Prometheus + Grafana** in the chart (`templates/prometheus.yaml`, `grafana.yaml`) — deployed
  and image-digest-pinned; the residual is a `node-exporter` DaemonSet for thermal/SMART, the
  `grafana.local` DNS entry, and confirming the API-scrape ipBlock matches your LAN.
- ✅ **`claimx` worker** now in the chart (`templates/claimx.yaml`, `claimx.enabled=true`) — the
  in-cluster Ollama client (role=inference, no DB), pinned to `qwen3:1.7b`. The summarize/entity-res/
  brief 4B tier (its own worker + model) stays deferred.
- **Pinned versions** recorded for: k3s, Longhorn, `pironman5` daemon, Ollama image (✅ digest-pinned
  in values) + **model** digests.
- **`pironman5` daemon** pinned + zero-egress-verified (also a standing supply-chain item).
- CI action refs are tag-pinned; **SHA-pinning them** is a tracked 0b hardening item.
