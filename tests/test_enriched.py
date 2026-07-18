"""EnrichedItem schema tests (PLAN §3.3 inference/db-writer seam)."""

from pydantic import ValidationError

from libs.schema import EMBED_DIM, EnrichedItem, Item, SourceClass


def _item() -> Item:
    return Item(source="usgs", source_class=SourceClass.AUTHORITATIVE, text="M5 quake")


def test_json_roundtrip() -> None:
    e = EnrichedItem(item=_item(), embedding=[0.1] * EMBED_DIM)
    back = EnrichedItem.model_validate_json(e.model_dump_json())
    assert back.item.text == "M5 quake"
    assert len(back.embedding) == EMBED_DIM


def test_rejects_wrong_embedding_length() -> None:
    for bad in (EMBED_DIM - 1, EMBED_DIM + 1):
        try:
            EnrichedItem(item=_item(), embedding=[0.0] * bad)
        except ValidationError:
            continue
        raise AssertionError(f"expected ValidationError for embedding length {bad}")
