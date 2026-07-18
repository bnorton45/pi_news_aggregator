# Shared image for ingest/enrich services (PLAN §3.2).
# Build arg EXTRA selects the per-zone dependency set so images stay minimal.
#
# NOTE: this slim+nonroot image is the fast inner-loop target. The production
# hardening (distroless/scratch base, read-only rootfs, dropped caps) is applied
# by the CI build + the k8s securityContext (deploy/policies); see PLAN §3.2/§3.4.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

ARG EXTRA=ingest

COPY pyproject.toml ./
COPY libs ./libs
COPY services ./services

RUN pip install --no-cache-dir ".[${EXTRA}]"

# Optional model bake (PLAN §6.3 step 3/4): only the enrich image sets BAKE_MODELS=1
# (build.yml matrix). Fetches the sha-pinned int8 ONNX encoders into /opt/models at
# BUILD time — where egress is allowed — so the runtime pod needs ZERO egress (the design
# invariant): no PVC to seed, no init-container fetch. Every other role defaults
# BAKE_MODELS=0, so this is a no-op layer for them. Baked files are root-owned and
# world-readable, so UID 10001 reads them under the read-only rootfs.
ARG BAKE_MODELS=0
COPY scripts/fetch-models.sh ./scripts/fetch-models.sh
RUN if [ "$BAKE_MODELS" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
      MODELS_DIR=/opt/models bash scripts/fetch-models.sh && \
      apt-get purge -y --auto-remove curl && \
      rm -rf /var/lib/apt/lists/*; \
    fi

# Nonroot, pinned high UID (PLAN §3.2). Code is owned by root and world-readable;
# the process runs unprivileged and writes nothing to the image filesystem.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser
USER 10001

# `command` is supplied by compose / the k8s manifest per service.
