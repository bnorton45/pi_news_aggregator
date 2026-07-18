"""Cheap filter (PLAN §6.3 step 2): gazetteer tagging + dedup + relevance in [0,1].

The relevance score feeds the admission governor (§6.3a). This is the cheap,
pre-embed stage; borderline items escalate to the **local small-LLM** — all
on-cluster, zero internet egress (PLAN §6.3).

Three pieces wired in here:
  * `tally()` runs the firehose **gazetteer matcher** on EVERY item, pre-embed, to
    populate coarse entities/geo and per-entity mention counts for velocity (§6.6) —
    a firehose-wide signal, separate from the survivor-only NER of §6.3 step 4.
  * `is_duplicate()` delegates to a shared **dedup store** (NATS KV in prod), so the
    exact/near-dup collapse is correct across classifier replicas.
  * `relevance()` scores via the **text-only classifier** (libs/classify), composed
    with the cheap gazetteer signals (geo/entities) already on the item.
"""

from __future__ import annotations

from collections import Counter

from libs.classify import Classifier, load_classifier
from libs.dedup import Deduper, InMemoryDeduper
from libs.gazetteer import Gazetteer, load_gazetteer
from libs.schema import Item, merge_entities


class CheapFilter:
    def __init__(
        self,
        deduper: Deduper | None = None,
        gazetteer: Gazetteer | None = None,
        classifier: Classifier | None = None,
    ) -> None:
        self._dedup = deduper or InMemoryDeduper()
        self._gaz = gazetteer or load_gazetteer()
        self._classifier = classifier or load_classifier()
        self.mentions: Counter[str] = Counter()  # per-entity firehose tally for velocity (§6.6)

    def swap_classifier(self, classifier: Classifier) -> None:
        """Hot-swap the relevance model (PLAN §6.3 step 2): the retrain loop publishes a
        new ONNX artifact and the model-watch task installs it here, live, with no restart.
        A plain attribute assignment is atomic under the GIL — in-flight `relevance()`
        calls finish on whichever model they read."""
        self._classifier = classifier

    def _key(self, item: Item) -> str:
        return item.content_hash or item.text  # exact-dedup key

    def tally(self, item: Item) -> None:
        """Gazetteer entity/geo tagging on EVERY item, pre-embed (PLAN §6.3 step 2, §6.6).

        Populates coarse entities for firehose items that have none (there is no NER
        yet at this stage) and accumulates per-entity mention counts. Runs BEFORE dedup
        so a copypasta burst still registers as mentions (velocity must see it).
        """
        hits = self._gaz.match(item.text)
        for ent in hits:
            # Casefolded like stories.entity_set (cluster/trust do the same), so the
            # score worker's tally lookup joins on identical keys (§6.6).
            self.mentions[ent.text.casefold()] += 1  # every occurrence counts
        merge_entities(item, hits)  # unique entities + coarse geo onto the item

    def drain_mentions(self) -> Counter[str]:
        """Hand off the accumulated tally and start a fresh window (§6.6 flush):
        each flush is a per-window DELTA, so the persisted buckets sum correctly."""
        out, self.mentions = self.mentions, Counter()
        return out

    async def is_duplicate(self, item: Item) -> bool:
        return await self._dedup.seen(self._key(item))

    def relevance(self, item: Item) -> float:
        """Relevance in [0,1] for the §6.3a gate: the text-only classifier score plus the
        cheap gazetteer signals already on the item (geo/entities from `tally`)."""
        score = self._classifier.score(item.text)
        if item.geo is not None:
            score += 0.15
        if item.entities:
            score += 0.15
        return max(0.0, min(1.0, score))
