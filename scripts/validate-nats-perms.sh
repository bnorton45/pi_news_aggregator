#!/usr/bin/env bash
# Live-broker validation of deploy/policies/nats-accounts.conf (see its header note).
# Starts a throwaway nats-server from the authored config with dummy creds, runs the
# per-user permission suite, tears down. Needs docker + the repo venv.
set -euo pipefail
cd "$(dirname "$0")/.."

docker run -d --name natsperm --rm -p 127.0.0.1:4222:4222 --tmpfs /data \
  -v "$PWD/deploy/policies/nats-accounts.conf:/tmp/nats.conf:ro" \
  -e NATS_PASS_SYS=x -e NATS_PASS_USGS=x -e NATS_PASS_NOAA=x -e NATS_PASS_GDACS=x \
  -e NATS_PASS_WIKIPEDIA=x -e NATS_PASS_BLUESKY=x -e NATS_PASS_MASTODON=x \
  -e NATS_PASS_GDELT=x -e NATS_PASS_ENRICH=x \
  -e NATS_PASS_WRITER=x -e NATS_PASS_CLUSTER=x -e NATS_PASS_CLAIMX=x -e NATS_PASS_TRUST=x \
  -e NATS_PASS_RETRAIN=x \
  nats:2.10-alpine -c /tmp/nats.conf >/dev/null
trap 'docker stop natsperm >/dev/null 2>&1 || true' EXIT
sleep 2
.venv/bin/python scripts/validate_nats_perms.py
