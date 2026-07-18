"""U.S. Dept of State press-release RSS -> Item.

Thin binding over the shared gov-RSS normalizer (`services.ingest.press`): State is
a standard RSS 2.0 press feed, AUTHORITATIVE class (PLAN §6.1). Kept as a named module
so the source reads self-contained and its tests target a stable import path.
"""

from __future__ import annotations

from typing import Any

from libs.schema import Item, SourceClass
from services.ingest.press import dedup_key, rss_to_item

SOURCE = "state"

__all__ = ["SOURCE", "dedup_key", "normalize"]


def normalize(item: dict[str, Any]) -> Item | None:
    return rss_to_item(item, source=SOURCE, source_class=SourceClass.AUTHORITATIVE)
