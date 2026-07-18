"""Independent-origin counting (PLAN §6.5) — pure, DB-free, unit-testable.

Independent origins = weakly-connected components of the Story's provenance
graph, further deduped by distinct org/domain: components that share an
origin key (author account, outlet domain, or the feed org itself for
non-social sources — see ProvNode.origin_key) collapse into one origin, so
N items from the same outlet or authority never count as N origins even if
edge detection missed a pairwise link. We count origins, never raw items —
that is what defeats circular reporting and single-source amplification.
"""

from __future__ import annotations

from uuid import UUID

from libs.trust.edges import ProvEdge, ProvNode


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[UUID, UUID] = {}

    def add(self, x: UUID) -> None:
        self._parent.setdefault(x, x)

    def find(self, x: UUID) -> UUID:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: UUID, b: UUID) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb

    def components(self) -> dict[UUID, set[UUID]]:
        out: dict[UUID, set[UUID]] = {}
        for x in self._parent:
            out.setdefault(self.find(x), set()).add(x)
        return out


def _origin_components(nodes: list[ProvNode], edges: list[ProvEdge]) -> list[set[UUID]]:
    """Group Items into independent-origin sets: weakly-connected components of the
    dependency graph, then collapse components sharing an author_ref/org/domain/wire
    key. Empty story → []."""
    if not nodes:
        return []
    uf = _UnionFind()
    known = {n.item_id for n in nodes}
    for n in nodes:
        uf.add(n.item_id)
    for e in edges:
        # Edges may reference members that already aged out of the window.
        if e.src in known and e.dst in known:
            uf.union(e.src, e.dst)

    origin_of_key: dict[str, UUID] = {}
    for n in nodes:
        key = n.origin_key
        if not key:
            continue
        root = uf.find(n.item_id)
        if key in origin_of_key:
            uf.union(root, origin_of_key[key])
        else:
            origin_of_key[key] = root

    return list(uf.components().values())


def independent_origins(nodes: list[ProvNode], edges: list[ProvEdge]) -> int:
    """Count weakly-connected components, then collapse components sharing an
    author_ref/org/domain. Empty story → 0."""
    return len(_origin_components(nodes, edges))


def distinct_origin_sources(nodes: list[ProvNode], edges: list[ProvEdge]) -> int:
    """Number of distinct *sources* (feeds/platforms) spanned by the independent
    origins (PLAN §6.5). Computed at the **origin level, not the item level**: each
    origin contributes ONE representative source (post org/wire collapse), so mass
    wire-syndication that collapses to a single origin also counts as a single
    source — corroboration must span ≥2 real sources, never N amplifications of one.
    Empty story → 0.

    `source` is the *ingest feed* (bluesky/mastodon/usgs/gdelt/…), so an aggregator
    like GDELT muxes many outlets under one source. That is intentional and harmless:
    a story carried only by mainstream outlets saturates `mainstream_presence`
    (§6.6, MAINSTREAM_SATURATION) → gap 0 regardless of trust state, while any story
    mixing social with even one non-social feed already spans ≥2 sources. Do NOT
    switch this to `author_ref`/`origin_key` — that would let N cheap social accounts
    (the exact amplification threat) each read as a distinct source again."""
    by_id = {n.item_id: n for n in nodes}
    sources: set[str] = set()
    for comp in _origin_components(nodes, edges):
        comp_sources = sorted(s for s in (by_id[i].source for i in comp) if s)
        if comp_sources:
            sources.add(comp_sources[0])  # deterministic representative per origin
    return len(sources)
