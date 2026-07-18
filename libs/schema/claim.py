"""llm.heavy queue messages (PLAN §6.3 tiering, §6.4).

The cluster service publishes a ClaimRequest on `llm.heavy` for members of *candidate*
Stories only (≥2 independent origins); the claim-extract worker (services/cluster/claimx)
runs the local Qwen3-4B on the text and emits a ClaimResult. The 4B never sees the full
survivor stream — only this gated queue (PLAN §6.3).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from libs.schema.item import MAX_TEXT_LEN


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    story_id: UUID
    item_id: UUID
    text: str = Field(max_length=MAX_TEXT_LEN)


class ClaimResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    story_id: UUID
    item_id: UUID
    claim: str = Field(default="", max_length=2048)  # the extracted factual assertion
