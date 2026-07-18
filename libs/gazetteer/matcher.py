"""Firehose gazetteer matcher (PLAN §6.3 step 2, §6.6).

A CHEAP, pure-function entity/geo tagger run on the *full firehose* to produce the
mention tallies + coarse geo that velocity (§6.6) needs — a firehose-wide signal,
deliberately separate from the model-based NER of §6.3 step 4 (post-filter, survivors
only). Longest-match, non-overlapping, case-insensitive over a curated gazetteer.

The matcher here is a simple token-window scan: fine for a curated seed; swap the
internals for an Aho-Corasick automaton (e.g. pyahocorasick) at firehose throughput
(PLAN §6.3). The gazetteer is curated reference data, so it is wall-compatible
(§4) — it is NOT retained firehose content.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

from libs.schema import Entity, EntityType, Geo

log = logging.getLogger("gazetteer")

_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class GazEntry:
    canonical: str
    type: EntityType
    lat: float | None = None
    lon: float | None = None

    def to_entity(self) -> Entity:
        geo = None
        if self.lat is not None and self.lon is not None:
            geo = Geo(lat=self.lat, lon=self.lon)
        return Entity(text=self.canonical, type=self.type, geo=geo)


# Curated seed — grow via GAZETTEER_PATH. Keys are lowercased phrases.
_SEED: dict[str, GazEntry] = {
    "tokyo": GazEntry("Tokyo", EntityType.PLACE, 35.6762, 139.6503),
    "san francisco": GazEntry("San Francisco", EntityType.PLACE, 37.7749, -122.4194),
    "new york": GazEntry("New York", EntityType.PLACE, 40.7128, -74.0060),
    "kyiv": GazEntry("Kyiv", EntityType.PLACE, 50.4501, 30.5234),
    "gaza": GazEntry("Gaza", EntityType.PLACE, 31.5, 34.47),
    "united nations": GazEntry("United Nations", EntityType.ORG),
    "world health organization": GazEntry("World Health Organization", EntityType.ORG),
}


class Gazetteer:
    def __init__(self, entries: dict[str, GazEntry]) -> None:
        self._gaz = entries
        self._max_words = max((len(p.split()) for p in entries), default=1)

    def match(self, text: str) -> list[Entity]:
        """All gazetteer hits in `text` (occurrences, not unique) — longest, non-overlapping."""
        tokens = _WORD.findall(text.lower())
        hits: list[Entity] = []
        i, n = 0, len(tokens)
        while i < n:
            step = 1
            for length in range(min(self._max_words, n - i), 0, -1):
                entry = self._gaz.get(" ".join(tokens[i : i + length]))
                if entry is not None:
                    hits.append(entry.to_entity())
                    step = length
                    break
            i += step
        return hits


def load_gazetteer() -> Gazetteer:
    """Built-in seed, optionally merged with a JSON list from GAZETTEER_PATH:
    ``[{"phrase","type","canonical","lat","lon"}]``."""
    entries = dict(_SEED)
    path = os.environ.get("GAZETTEER_PATH")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                for row in json.load(fh):
                    entries[row["phrase"].lower()] = GazEntry(
                        canonical=row.get("canonical", row["phrase"]),
                        type=EntityType(row.get("type", "other")),
                        lat=row.get("lat"),
                        lon=row.get("lon"),
                    )
        except Exception:
            log.exception("failed to load GAZETTEER_PATH=%s; using seed only", path)
    return Gazetteer(entries)
