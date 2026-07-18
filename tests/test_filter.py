"""CheapFilter wiring tests (PLAN §6.3 step 2): gazetteer tally + async dedup."""

from libs.schema import Item, SourceClass
from services.enrich.filter import CheapFilter


def _social(text: str, content_hash: str = "") -> Item:
    return Item(
        source="bluesky", source_class=SourceClass.SOCIAL, text=text, content_hash=content_hash
    )


def test_tally_populates_entities_geo_and_mentions() -> None:
    f = CheapFilter()
    item = _social("Explosion reported in Tokyo near Tokyo bay")
    f.tally(item)
    assert any(e.text == "Tokyo" for e in item.entities)  # coarse entity populated
    assert item.geo is not None  # coarse geo from the place hit
    assert f.mentions["tokyo"] == 2  # both occurrences counted, casefolded key (§6.6)


def test_tally_does_not_duplicate_existing_entities() -> None:
    f = CheapFilter()
    item = _social("Tokyo Tokyo Tokyo")
    f.tally(item)
    assert [e.text for e in item.entities].count("Tokyo") == 1  # entity list deduped
    assert f.mentions["tokyo"] == 3  # tally still counts every mention


async def test_is_duplicate_uses_dedup_store() -> None:
    f = CheapFilter()
    item = _social("same text", content_hash="h1")
    assert await f.is_duplicate(item) is False
    assert await f.is_duplicate(item) is True


def test_relevance_combines_classifier_and_gazetteer() -> None:
    f = CheapFilter()
    newsy = _social("Breaking: explosion reported in Tokyo, several killed", "r1")
    f.tally(newsy)  # populates Tokyo entity + geo
    bland = _social("thinking about lunch options", "r2")
    f.tally(bland)
    assert f.relevance(newsy) > f.relevance(bland)
    assert 0.0 <= f.relevance(bland) <= 1.0
    assert 0.0 <= f.relevance(newsy) <= 1.0
