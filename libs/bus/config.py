"""Bus configuration (PLAN §3.2, §4).

Account creds + URL come from the environment so the same image runs against a
local compose NATS (no creds) or an account-scoped cluster NATS (per-zone creds).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_MAX_AGE_DAYS = 5  # PLAN §4 hard wall — JetStream auto-expires at 5 days
DEFAULT_MAX_MSG_BYTES = 64 * 1024  # boundary size cap (PLAN §3.2)
# An EnrichedItem wraps an Item (up to the cap above) PLUS its ~9KB embedding vector,
# so the inference->writer seam (enriched.*) needs headroom over the ingest cap or a
# max-size Item would produce an un-publishable message (PLAN §3.3 split).
ENRICHED_MAX_MSG_BYTES = DEFAULT_MAX_MSG_BYTES + 16 * 1024


@dataclass(frozen=True)
class BusConfig:
    url: str
    creds: str | None  # path to a NATS .creds file (account-scoped); None = local/dev
    stream: str
    subjects: tuple[str, ...]
    max_age_days: int
    max_msg_bytes: int

    @classmethod
    def from_env(
        cls,
        *,
        stream: str = "INGEST",
        subjects: tuple[str, ...] = ("ingest.>",),
    ) -> BusConfig:
        return cls(
            url=os.environ.get("NATS_URL", "nats://localhost:4222"),
            creds=os.environ.get("NATS_CREDS") or None,
            stream=os.environ.get("NATS_STREAM", stream),
            subjects=tuple(
                s.strip()
                for s in os.environ.get("NATS_SUBJECTS", ",".join(subjects)).split(",")
                if s.strip()
            ),
            max_age_days=int(os.environ.get("NATS_MAX_AGE_DAYS", DEFAULT_MAX_AGE_DAYS)),
            max_msg_bytes=int(os.environ.get("NATS_MAX_MSG_BYTES", DEFAULT_MAX_MSG_BYTES)),
        )

    @property
    def max_age_seconds(self) -> float:
        return self.max_age_days * 24 * 60 * 60
