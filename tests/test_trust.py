"""Trust-layer unit tests (PLAN §6.5): edges, origin counting, state machine."""

from uuid import uuid4

from libs.dedup.simhash import from_signed64, hamming64, simhash64, to_signed64
from libs.trust import (
    EdgeType,
    ProvEdge,
    ProvNode,
    TrustState,
    canonical_url,
    detect_edges,
    distinct_origin_sources,
    independent_origins,
    next_state,
)


def node(
    author: str = "",
    parent: str | None = None,
    chash: str = "",
    urls: set[str] | None = None,
    text: str = "",
    source: str = "",
    source_class: str = "social",
    wire: str = "",
) -> ProvNode:
    return ProvNode(
        item_id=uuid4(),
        author_ref=author,
        parent_ref=parent,
        content_hash=chash or f"h:{uuid4().hex[:12]}",
        urls=frozenset(canonical_url(u) for u in (urls or set())),
        simhash=simhash64(text),
        source=source,
        source_class=source_class,
        wire_ref=wire,
    )


# ── simhash ───────────────────────────────────────────────────────────────────


def test_simhash_identical_and_near() -> None:
    a = "Breaking: strong earthquake hits the coastal region tonight, buildings shaking"
    b = "Breaking: strong earthquake hits the coastal region tonight, buildings shaking!!"
    c = "Local council approves new bicycle lanes after long public consultation period"
    assert simhash64(a) == simhash64(a)
    assert hamming64(simhash64(a), simhash64(b)) <= 6
    assert hamming64(simhash64(a), simhash64(c)) > 6


def test_simhash_empty_is_zero() -> None:
    assert simhash64("") == 0
    assert simhash64("   ") == 0


def test_signed64_roundtrip() -> None:
    for v in (0, 1, 2**63 - 1, 2**63, 2**64 - 1):
        assert from_signed64(to_signed64(v)) == v
        assert -(2**63) <= to_signed64(v) <= 2**63 - 1


# ── canonical URL ─────────────────────────────────────────────────────────────


def test_canonical_url_strips_noise() -> None:
    assert (
        canonical_url("https://www.Example.com/a/b/?utm_source=x&id=7")
        == canonical_url("http://example.com/a/b?id=7#frag")
        != ""
    )


def test_canonical_url_unusable_input() -> None:
    assert canonical_url("not a url") == ""
    assert canonical_url("") == ""


# ── edge detection ────────────────────────────────────────────────────────────


def test_ref_edge_parent_to_content_hash() -> None:
    parent = node(author="a1", chash="bsky:abc")
    reply = node(author="a2", parent="bsky:abc")
    edges = detect_edges(reply, [parent])
    assert [e.edge_type for e in edges] == [EdgeType.REF]


def test_copy_edge_copypasta() -> None:
    text = "URGENT: dam failure reported upstream of the city, evacuate low areas now"
    a = node(author="a1", text=text)
    b = node(author="a2", text=text + " !!")
    assert EdgeType.COPY in {e.edge_type for e in detect_edges(b, [a])}


def test_url_edge_shared_canonical() -> None:
    a = node(author="a1", urls={"https://www.outlet.com/story?utm_source=t"})
    b = node(author="a2", urls={"http://outlet.com/story"})
    assert EdgeType.URL in {e.edge_type for e in detect_edges(b, [a])}


def test_author_edge_same_ref_and_empty_never_matches() -> None:
    a, b = node(author="acct:x"), node(author="acct:x")
    assert EdgeType.AUTHOR in {e.edge_type for e in detect_edges(b, [a])}
    e1, e2 = node(author=""), node(author="")
    assert detect_edges(e2, [e1]) == []


def test_independent_items_no_edges() -> None:
    a = node(author="a1", urls={"https://x.example/1"}, text="one thing happened")
    b = node(author="a2", urls={"https://y.example/2"}, text="a different event entirely")
    assert detect_edges(b, [a]) == []


# ── origin counting ───────────────────────────────────────────────────────────


def test_origins_edges_collapse_components() -> None:
    a, b, c = node(author="a1"), node(author="a2"), node(author="a3")
    edges = [ProvEdge(a.item_id, b.item_id, EdgeType.REF)]
    assert independent_origins([a, b, c], edges) == 2


def test_origins_author_dedup_without_edge() -> None:
    # Same outlet twice with no pairwise edge recorded still counts once (§6.5
    # "deduped by distinct org/domain").
    a, b, c = node(author="outlet.com"), node(author="outlet.com"), node(author="other.org")
    assert independent_origins([a, b, c], []) == 2


def test_origins_ignores_out_of_window_edge_endpoints() -> None:
    a, b = node(author="a1"), node(author="a2")
    ghost = uuid4()  # aged-out member referenced by a stale edge
    edges = [ProvEdge(a.item_id, ghost, EdgeType.REF)]
    assert independent_origins([a, b], edges) == 2


def test_origins_empty_story() -> None:
    assert independent_origins([], []) == 0


def test_origins_authoritative_feed_is_one_org() -> None:
    # Two USGS events in one story: no author_ref, no shared URL — still ONE origin
    # (the feed is the org; keeps CI's single-source llm.heavy-stays-closed assert true).
    a = node(source="usgs", source_class="authoritative", urls={"https://usgs.gov/ev/1"})
    b = node(source="usgs", source_class="authoritative", urls={"https://usgs.gov/ev/2"})
    assert independent_origins([a, b], []) == 1


def test_origins_anonymous_social_never_merges() -> None:
    a = node(source="mastodon", source_class="social")
    b = node(source="mastodon", source_class="social")
    assert independent_origins([a, b], []) == 2


def test_origins_mixed_classes() -> None:
    usgs1 = node(source="usgs", source_class="authoritative")
    usgs2 = node(source="usgs", source_class="authoritative")
    outlet = node(author="outlet.com", source="gdelt", source_class="mainstream")
    social = node(author="acct:x", source="bluesky", source_class="social")
    assert independent_origins([usgs1, usgs2, outlet, social], []) == 3


# ── wire-syndication collapse (§6.5, docs/local-news-design.md) ──────────────


def test_wire_stations_are_one_origin() -> None:
    # Five stations, five different localizations of one AP story: texts differ
    # (simhash won't link them), authors/sources differ — the wire key alone must
    # fold them into ONE origin, or mass syndication forges corroboration.
    stations = [
        node(
            author=f"station{i}.com",
            source=f"station{i}",
            source_class="local_news",
            text=f"CITY {i}, St. (AP) — localized lede number {i} about the flood downtown.",
            wire="ap",
        )
        for i in range(5)
    ]
    assert independent_origins(stations, []) == 1


def test_wire_plus_original_reporting_counts_separately() -> None:
    ap1 = node(author="kwch.com", source="kwch", source_class="local_news", wire="ap")
    ap2 = node(author="kake.com", source="kake", source_class="local_news", wire="ap")
    original = node(author="ksn.com", source="ksn", source_class="local_news")  # own reporting
    usgs = node(source="usgs", source_class="authoritative")
    # AP lineage (1) + original station reporting (1) + authority (1) = 3 → can corroborate.
    assert independent_origins([ap1, ap2, original, usgs], []) == 3


def test_wire_collapse_spans_local_and_mainstream() -> None:
    station = node(author="kwch.com", source="kwch", source_class="local_news", wire="ap")
    national = node(author="outlet.com", source="gdelt", source_class="mainstream", wire="ap")
    assert independent_origins([station, national], []) == 1


def test_social_never_wire_collapses() -> None:
    # Two accounts pasting AP copy: wire_ref may be detected on the text, but for
    # social the key is ignored — each account is its own act of amplification
    # (COPY edges still catch verbatim reposts pairwise).
    a = node(source="bluesky", source_class="social", wire="ap")
    b = node(source="mastodon", source_class="social", wire="ap")
    assert independent_origins([a, b], []) == 2


def test_wire_beats_author_key_for_outlets() -> None:
    n = node(author="kwch.com", source="kwch", source_class="local_news", wire="ap")
    assert n.origin_key == "wire:ap"
    plain = node(author="kwch.com", source="kwch", source_class="local_news")
    assert plain.origin_key == "kwch.com"


# ── origin source diversity (§6.5 corroboration floor) ──────────────────────────


def test_source_diversity_single_platform_is_one_source() -> None:
    # Three distinct Bluesky accounts = 3 origins but ONE source: cannot corroborate.
    a = node(author="acct:x", source="bluesky", source_class="social")
    b = node(author="acct:y", source="bluesky", source_class="social")
    c = node(author="acct:z", source="bluesky", source_class="social")
    assert independent_origins([a, b, c], []) == 3
    assert distinct_origin_sources([a, b, c], []) == 1


def test_source_diversity_cross_platform_social() -> None:
    a = node(author="acct:x", source="bluesky", source_class="social")
    b = node(author="acct:y", source="mastodon", source_class="social")
    assert distinct_origin_sources([a, b], []) == 2


def test_source_diversity_counts_at_origin_level_not_item_level() -> None:
    # Five wire-syndicated stations collapse to ONE origin → ONE source, even though
    # the five items carry five different `source` values (mass syndication must not
    # forge source diversity any more than it forges origin count).
    stations = [
        node(
            author=f"station{i}.com",
            source=f"station{i}",
            source_class="local_news",
            text=f"CITY {i}, St. (AP) — localized lede {i} about the flood downtown.",
            wire="ap",
        )
        for i in range(5)
    ]
    assert independent_origins(stations, []) == 1
    assert distinct_origin_sources(stations, []) == 1


def test_source_diversity_social_plus_one_feed_corroborates() -> None:
    # The gap-mission guard: a mostly-social story with even ONE non-social feed
    # spans 2 sources and can corroborate before mainstream catches up.
    a = node(author="acct:x", source="bluesky", source_class="social")
    b = node(author="acct:y", source="bluesky", source_class="social")
    usgs = node(source="usgs", source_class="authoritative", urls={"https://usgs.gov/ev/1"})
    assert independent_origins([a, b, usgs], []) == 3
    assert distinct_origin_sources([a, b, usgs], []) == 2


def test_source_diversity_empty_story() -> None:
    assert distinct_origin_sources([], []) == 0


# ── state machine ─────────────────────────────────────────────────────────────


def test_states_thresholds() -> None:
    assert next_state(TrustState.RUMOR, 1) is TrustState.RUMOR
    assert next_state(TrustState.RUMOR, 2) is TrustState.RUMOR
    assert next_state(TrustState.RUMOR, 3) is TrustState.CORROBORATED
    assert next_state(TrustState.RUMOR, 2, n=2) is TrustState.CORROBORATED
    assert next_state(TrustState.RUMOR, 1, primary_matched=True) is TrustState.PRIMARY_BACKED


def test_states_source_diversity_floor() -> None:
    # Enough origins but a single source → stays RUMOR (single-platform amplification).
    assert next_state(TrustState.RUMOR, 3, distinct_sources=1) is TrustState.RUMOR
    assert next_state(TrustState.RUMOR, 5, distinct_sources=1) is TrustState.RUMOR
    # Enough origins AND ≥2 sources → CORROBORATED.
    assert next_state(TrustState.RUMOR, 3, distinct_sources=2) is TrustState.CORROBORATED
    # The floor never blocks a primary match (inherently social ∧ primary = 2 sources).
    assert (
        next_state(TrustState.RUMOR, 1, distinct_sources=1, primary_matched=True)
        is TrustState.PRIMARY_BACKED
    )


def test_states_monotonic_no_demotion() -> None:
    assert next_state(TrustState.CORROBORATED, 1) is TrustState.CORROBORATED
    assert next_state(TrustState.PRIMARY_BACKED, 1) is TrustState.PRIMARY_BACKED
    assert next_state(TrustState.PRIMARY_BACKED, 5) is TrustState.PRIMARY_BACKED
