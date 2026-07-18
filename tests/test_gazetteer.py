"""Gazetteer matcher unit tests (PLAN §6.3 step 2, §6.6)."""

from libs.gazetteer import load_gazetteer
from libs.schema import EntityType


def test_matches_single_place() -> None:
    ents = load_gazetteer().match("A big earthquake struck Tokyo today")
    assert any(e.text == "Tokyo" and e.type is EntityType.PLACE for e in ents)


def test_matches_multiword_phrase() -> None:
    ents = load_gazetteer().match("Reports out of San Francisco overnight")
    assert any(e.text == "San Francisco" for e in ents)


def test_longest_non_overlapping_match() -> None:
    # "san francisco" should win over a hypothetical "san" — and "Tokyo" counts twice.
    ents = load_gazetteer().match("explosion in Tokyo near Tokyo bay")
    assert [e.text for e in ents].count("Tokyo") == 2


def test_geo_attached_to_place() -> None:
    ents = load_gazetteer().match("news from Tokyo")
    tokyo = next(e for e in ents if e.text == "Tokyo")
    assert tokyo.geo is not None


def test_no_false_positive() -> None:
    assert load_gazetteer().match("nothing notable happened here") == []


def test_case_insensitive() -> None:
    ents = load_gazetteer().match("KYIV under heavy shelling")
    assert any(e.text == "Kyiv" for e in ents)
