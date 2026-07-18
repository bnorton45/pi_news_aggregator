"""Primary-match tests (PLAN §6.5): entity ∧ geo ∧ time alignment rules."""

from uuid import uuid4

from services.trust.match import GEO_KM_MAX, MatchCandidate, find_primary_match, haversine_km


def cand(entities: set[str], lat: float | None = None, lon: float | None = None) -> MatchCandidate:
    return MatchCandidate(
        item_id=uuid4(), entity_texts=frozenset(e.casefold() for e in entities), lat=lat, lon=lon
    )


def test_entity_overlap_matches() -> None:
    c = cand({"Guatemala"}, lat=14.6, lon=-90.5)
    assert find_primary_match({"guatemala", "earthquake"}, None, None, [c]) == c.item_id


def test_no_shared_entity_no_match() -> None:
    c = cand({"Tokyo"})
    assert find_primary_match({"guatemala"}, None, None, [c]) is None


def test_geo_veto_when_both_sides_have_coords() -> None:
    far = cand({"earthquake"}, lat=35.7, lon=139.7)  # Tokyo
    near = cand({"earthquake"}, lat=14.5, lon=-90.6)
    got = find_primary_match({"earthquake"}, 14.6, -90.5, [far, near])
    assert got == near.item_id  # far candidate vetoed by distance, near one matches


def test_one_sided_geo_does_not_veto() -> None:
    c = cand({"flood"}, lat=51.0, lon=4.0)
    assert find_primary_match({"flood"}, None, None, [c]) == c.item_id


def test_empty_claim_entities_never_match() -> None:
    c = cand({"anything"})
    assert find_primary_match(set(), None, None, [c]) is None
    assert find_primary_match({""}, None, None, [c]) is None


def test_haversine_sanity() -> None:
    assert haversine_km(0, 0, 0, 0) == 0
    assert abs(haversine_km(50.0, 4.0, 50.0, 5.0) - 71.5) < 2  # ~1° lon at 50°N
    assert haversine_km(35.7, 139.7, 14.6, -90.5) > GEO_KM_MAX  # Tokyo↔Guatemala
