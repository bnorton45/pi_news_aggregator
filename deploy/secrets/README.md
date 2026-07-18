# deploy/secrets — SOPS-encrypted material (PLAN §3.5–§3.6)

The ONLY externally-originating secret in the system is the **GHCR pull token**; it
lives here SOPS+age-encrypted as `ghcr-pull.sops.yaml` (never committed plaintext —
gitleaks/detect-secrets/CI gates enforce this). Everything else (Postgres password,
NATS user passwords) is **generated in-cluster** by the Helm chart and never exists
outside it.

Crown jewels — the **age private key** and the **Flux deploy key** — exist only
in-cluster + the password manager. Never here, encrypted or not.

## Creating the pull secret (bootstrap runbook, task 10 / 0b)

```bash
age-keygen -o age.key                       # once; private half -> cluster + pw manager
# put the public key in /.sops.yaml (replace the placeholder recipient)
kubectl -n flux-system create secret generic sops-age \
  --from-file=age.agekey=age.key            # Flux decryption key (in-cluster only)

# GHCR pull secret for both image-pulling zones, then encrypt in place:
for ns in zone-ingest zone-process; do
  kubectl -n "$ns" create secret docker-registry ghcr-pull \
    --docker-server=ghcr.io --docker-username=bnorton45 \
    --docker-password="$GHCR_TOKEN" --dry-run=client -o yaml
done > ghcr-pull.sops.yaml
sops --encrypt --in-place ghcr-pull.sops.yaml
```

`deploy/flux/secrets-sync.yaml` reconciles this directory with SOPS decryption; the dev
(k3d) loop skips it entirely (`imagePullSecret: ""` in values-dev).
