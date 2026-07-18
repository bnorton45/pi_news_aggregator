"""Per-survivor NER tests (PLAN §6.3 step 4)."""

from libs.ner import NoOpNer, load_ner


def test_noop_returns_no_entities() -> None:
    # 0a fallback: entities come from the gazetteer tally, not a model.
    assert NoOpNer().extract("explosion reported in Tokyo") == []


def test_load_default_is_noop() -> None:
    assert isinstance(load_ner(), NoOpNer)
