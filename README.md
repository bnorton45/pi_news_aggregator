# OSINT Developing-Story Aggregator

Surfaces *developing* stories from open sources — fast, but only after corroboration.
See [PLAN.md](./PLAN.md) for the full architecture, security model, and data-lifecycle rules.

> **Hard rules:** corroboration-first · ephemeral by design (5-day data wall) ·
> compromise-resilient (trust-zone segmentation) · single user, global scope.

## Deployment

The full software stack runs end-to-end on a single machine via Docker Compose (see
[Quick start](#quick-start-inner-loop)). The throughput and capacity figures in
[`docs/pi-throughput-findings.md`](docs/pi-throughput-findings.md) were measured locally
on a dev box.

Running the system on real hardware — provisioning the 3× Pi 5 cluster (OS, networking,
Longhorn storage, pinned node daemon) and bootstrapping it — is the operator's
responsibility. [`docs/0b-bootstrap-runbook.md`](docs/0b-bootstrap-runbook.md) documents
the suggested steps.

## Validation

| Validation | Where |
|---|---|
| unit tests + compose inner loop | `pytest -q`, `docker compose up --build` |
| k8s fidelity — GitOps flow, PSS, netpol denials, NATS ACLs, SOPS round-trip, full pipeline + retrain hot-swap | CI `k3d-e2e.yml` (single-node) |
| 3-node topology — `role` placement, PriorityClass preemption, compute-node drain-failover | CI `k3d-multinode-e2e.yml` |
| arm64 (Pi 5) — images build AND import onnxruntime/onnx/asyncpg/numpy on aarch64 | CI `arm64-smoke.yml` (QEMU) |

## Quick start (inner loop)

```bash
docker compose up --build        # NATS + Postgres/pgvector + USGS ingester + enrich(inference) + writer + cluster
docker compose exec postgres psql -U app -d news -c "select source, count(*) from items group by 1;"
```

## Layout

See [PLAN.md §8](./PLAN.md). `libs/` = shared (schema, bus, embed, dedup, gazetteer, classify, ner, llm);
`services/` = one hardened image per role; `deploy/` = Helm + Flux + policies + SOPS-encrypted secrets.

## Local secrets (`.env`)

Define dev secrets once in a gitignored `.env` instead of re-exporting each session:

```bash
cp .env.example .env          # then fill in GH_TOKEN etc.
direnv allow                  # auto-loads .env on cd  (or: source scripts/dev-env.sh)
git push                      # gh/git now see GH_TOKEN automatically
```

`.env` is gitignored and never committed; `.env.example` (placeholders only) is. This is
**dev convenience** — the production secret (GHCR pull token) does NOT come from `.env`; it is
SOPS-encrypted in `deploy/secrets/` and exists only in-cluster (PLAN §3.5). There is **no LLM API
key** — enrichment runs on a local in-cluster Ollama/Qwen3-4B service (zero egress, PLAN §3.3).

## Security

Secrets never enter git history (PLAN §3.6). `deploy/secrets/` holds **SOPS-encrypted only**.
The age private key and Flux deploy key live in-cluster + a password manager — never in the repo.

Three secret-scanning gates back this up:
1. `.gitignore` — env files, keys, kubeconfig, `*.age`, decrypted manifests.
2. **pre-commit** — gitleaks + detect-secrets block locally. Enable on a fresh clone:
   ```bash
   pip install pre-commit && pre-commit install   # gitleaks runs via its container image
   ```
3. **CI** — `.github/workflows/secret-scan.yml` scans every push/PR over **full history**.
