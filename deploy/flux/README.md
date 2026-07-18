# deploy/flux — GitOps reconciliation (Phase 0a task 8)

`flux bootstrap github --owner=bnorton45 --repository=news_aggregator --path=deploy/flux`
(read-only deploy key, PLAN §3.5) installs the controllers and reconciles this directory:

```
policies   ./deploy/policies          namespaces+PSS, netpol, RBAC   (first, wait=true)
secrets    ./deploy/secrets           SOPS-encrypted GHCR pull secret — SUSPENDED until
                                      bootstrap creates sops-age + ghcr-pull.sops.yaml,
                                      then `flux resume kustomization secrets`
apps       ./deploy/flux/apps         HelmRelease news-aggregator (dependsOn policies)
```

The HelmRelease renders in-cluster (helm-controller), which is what makes the chart's
lookup-based generate-or-preserve credentials work (PLAN §3.5). Dev parity on k3d
(task 10) can either run this same bootstrap against a fork/branch or install the
chart directly: `helm install na deploy/helm/news-aggregator -f .../values-dev.yaml`.
