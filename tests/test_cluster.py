"""Clustering decision logic tests (PLAN §6.4): merge vs new-Story, llm.heavy gate."""

from uuid import uuid4

from libs.schema import Entity
from services.cluster.cluster import Neighbor, choose_story, entity_texts, is_candidate

THETA = 0.82


def _neighbor(sim: float, *ents: str) -> Neighbor:
    return Neighbor(story_id=uuid4(), similarity=sim, entity_texts=frozenset(ents))


def test_entity_texts_casefolds() -> None:
    ents = [Entity(text="Tokyo"), Entity(text="WHO")]
    assert entity_texts(ents) == {"tokyo", "who"}


def test_merges_into_nearest_similar_story_sharing_an_entity() -> None:
    near = _neighbor(0.9, "tokyo")
    far = _neighbor(0.85, "tokyo")
    assert choose_story({"tokyo"}, [near, far], THETA) == near.story_id


def test_no_merge_below_theta() -> None:
    assert choose_story({"tokyo"}, [_neighbor(0.5, "tokyo")], THETA) is None


def test_no_merge_without_shared_entity_despite_high_similarity() -> None:
    # Coincidental vector proximity must not merge Stories (§6.4 strictness).
    assert choose_story({"kyiv"}, [_neighbor(0.99, "tokyo")], THETA) is None


def test_item_with_no_entities_opens_its_own_story() -> None:
    assert choose_story(set(), [_neighbor(0.99, "tokyo")], THETA) is None


def test_shared_entity_test_is_case_insensitive() -> None:
    n = _neighbor(0.9, "tokyo")
    assert choose_story({"TOKYO"}, [n], THETA) == n.story_id


def test_skips_nearer_nonmatching_neighbor_for_farther_matching_one() -> None:
    matching = _neighbor(0.85, "tokyo")
    assert choose_story({"tokyo"}, [_neighbor(0.95, "kyiv"), matching], THETA) == matching.story_id


def test_is_candidate_gate() -> None:
    # PLAN §6.3: the 4B only sees Stories with >=2 independent origins.
    assert not is_candidate(1)
    assert is_candidate(2)
    assert is_candidate(3)
    assert not is_candidate(2, threshold=3)


def test_in_story_primary_match() -> None:
    from services.cluster.cluster import in_story_primary_match

    assert in_story_primary_match({"social", "authoritative"})
    assert in_story_primary_match({"social", "primary", "mainstream"})
    assert not in_story_primary_match({"social", "mainstream"})
    assert not in_story_primary_match({"authoritative", "mainstream"})  # no social claim
    assert not in_story_primary_match(set())


def test_prov_node_from_row() -> None:
    import json
    from uuid import uuid4

    from services.cluster.cluster import prov_node

    iid = uuid4()
    row = {
        "id": iid,
        "author_ref": "outlet.com",
        "parent_ref": None,
        "content_hash": "gdelt:abc",
        "urls": json.dumps(["https://www.Outlet.com/x/?utm_source=t", "notaurl"]),
        "simhash": -1,  # signed bigint from pg
        "source": "gdelt",
        "source_class": "mainstream",
        "wire_ref": "",
    }
    n = prov_node(row)
    assert n.item_id == iid
    assert n.urls == frozenset({"outlet.com/x"})
    assert n.simhash == 2**64 - 1  # unsigned round-trip
    assert n.origin_key == "outlet.com"  # author_ref beats the source fallback

    # Wire attribution beats the author key for outlet classes (§6.5 collapse).
    n = prov_node({**row, "wire_ref": "ap"})
    assert n.origin_key == "wire:ap"
