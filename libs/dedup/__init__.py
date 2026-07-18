"""Dedup store (PLAN §6.3 step 2): exact + near-dup, shared across replicas in prod."""

from libs.dedup.dedup import Deduper, InMemoryDeduper, NatsKvDeduper, load_deduper

__all__ = ["Deduper", "InMemoryDeduper", "NatsKvDeduper", "load_deduper"]
