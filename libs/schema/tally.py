"""Firehose tally flush (PLAN §6.6): the velocity signal's transport schema.

Enrich replicas tally gazetteer mentions on EVERY item pre-admission (§6.3 step 2)
and flush per-minute deltas here; the db-writer persists them. Velocity therefore
sees the full firehose — adaptive admission control (§6.3a) cannot bias it, which
is exactly why this rides its own message instead of the EnrichedItem stream.

Like every NATS payload this is validated + size-capped at the boundary: entity
keys are attacker-derived surface forms (via the gazetteer's canonical output,
but still treat them as data, never identifiers).
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_TALLY_ENTITIES = 4_096
MAX_TALLY_KEY_LEN = 256


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TallyFlush(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bucket_ts: datetime  # minute bucket the counts belong to (UTC, floored)
    counts: dict[str, int] = Field(default_factory=dict)
    # Governor observability for the §6.8 health states, carried on the same beat:
    sampling_active: bool = False
    theta: float = Field(default=0.0, ge=0.0, le=1.0)
    replica: str = Field(default="", max_length=128)  # flush idempotency key part
    flushed_at: datetime = Field(default_factory=_utcnow)

    @field_validator("counts")
    @classmethod
    def _cap_counts(cls, v: dict[str, int]) -> dict[str, int]:
        if len(v) > MAX_TALLY_ENTITIES:
            raise ValueError(f"too many tally entities ({len(v)} > {MAX_TALLY_ENTITIES})")
        for k, n in v.items():
            if not k or len(k) > MAX_TALLY_KEY_LEN:
                raise ValueError("tally entity key empty or too long")
            if n < 0:
                raise ValueError("negative tally count")
        return v
