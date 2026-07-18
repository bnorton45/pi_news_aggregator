"""Relevance classifier tests (PLAN §6.3 step 2)."""

from libs.classify import HeuristicClassifier, load_classifier


def test_newsworthy_scores_higher_than_bland() -> None:
    c = HeuristicClassifier()
    assert c.score("Breaking: explosion reported downtown, several killed") > c.score(
        "had a nice sandwich for lunch today"
    )


def test_score_is_bounded() -> None:
    c = HeuristicClassifier()
    for text in ("", "x" * 200, "earthquake " * 50, "ordinary chatter"):
        assert 0.0 <= c.score(text) <= 1.0


def test_text_only_does_not_need_an_item() -> None:
    # The classifier takes raw text (no entities/geo) — that's the §6.3 contract.
    assert load_classifier().score("magnitude 7 earthquake near the coast") > 0.0


def test_load_default_is_heuristic() -> None:
    assert isinstance(load_classifier(), HeuristicClassifier)
