# deploy/policies — trust-zone enforcement (PLAN §3.1–§3.3)

Authored in Phase 0a, **validated on k3d in task 10** (no cluster on the dev box yet).
Applied by Flux before the Helm releases (task 8) so workloads land inside the fences.

| File | Enforces |
|---|---|
| `namespaces.yaml` | 4 trust zones, PSS `restricted` on all of them |
| `netpol-zone-*.yaml` | default-deny both directions per zone + the minimal allows below |
| `rbac.yaml` | zero kube-API power: no Roles at all, `automountServiceAccountToken: false` on every SA incl. each zone's `default` |
| `nats-accounts.conf` | broker-enforced per-service subject allow-lists (see header note) |

## Allowed flows (everything else is denied)

```
ingest(role=ingester) ──443──> internet (non-private CIDRs only)   [FQDN gap: see below]
ingest(role=ingester) ──4222─> zone-data nats
zone-process (all)    ──4222─> zone-data nats
role=db-writer|cluster ─5432─> zone-data postgres        (§3.3: inference pods have NO rule)
role=inference        ─11434─> zone-process ollama
zone-present role=api ──5432─> zone-data postgres        (read-only DB role)
kube-system traefik   ──8000─> zone-present api,  ──3000─> zone-data grafana
zone-data prometheus  ──> every zone's named `metrics` port, 6443 kube SD
all zones             ──53───> kube-system CoreDNS
```

Workload manifests (task 8) must carry the labels these policies match on:
`role: ingester|inference|db-writer|cluster|api` · `app: nats|postgres|ollama|prometheus|grafana`
and name their metrics container port `metrics`.

## Known gaps (tracked, deliberate)

1. **Per-ingester FQDN allow-list** (PLAN: "ONLY its upstream host") is not expressible
   in vanilla NetworkPolicy. 0a ships "443 to non-private space only" — blocks all
   lateral/LAN/metadata movement but not public egress. Fix candidates at 0b: Cilium
   `toFQDNs` (replaces k3s kube-router netpol) or a per-zone egress proxy.
2. **NATS**: one JetStream account with per-user allow-lists instead of one account per
   zone — cross-account stream sharing needs export/import plumbing with no containment
   gain here. Rationale in `nats-accounts.conf` header.
3. ~~The `$JS.API.*` permission lists must be validated against a live broker~~ —
   **done 2026-07-02**: `scripts/validate_nats_perms.py` runs every service user's real
   operation set (streams, durables, acks, KV dedup) plus key denials against a broker
   started from this exact config; 17/17 checks pass on nats-server 2.10 / nats-py 2.15.
   Re-run: `scripts/validate-nats-perms.sh` (needs docker + the venv).
