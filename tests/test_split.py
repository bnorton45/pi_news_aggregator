"""Inference worker tests (PLAN §3.3): admit -> publish EnrichedItem, with no DB."""

from libs.schema import EMBED_DIM, EnrichedItem, Item, SourceClass
from services.enrich.filter import CheapFilter
from services.enrich.main import Inference


class _CapturePublisher:
    """Stands in for ScopedPublisher — records what the worker would publish."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, EnrichedItem]] = []

    async def publish(self, subject: str, model: EnrichedItem) -> None:
        self.sent.append((subject, model))


def _social(text: str, content_hash: str) -> Item:
    return Item(
        source="bluesky", source_class=SourceClass.SOCIAL, text=text, content_hash=content_hash
    )


async def test_admitted_item_is_published_as_enriched() -> None:
    pub = _CapturePublisher()
    inf = Inference(CheapFilter(), pub)  # type: ignore[arg-type]
    await inf.handle(_social("Explosion reported in Tokyo tonight", "h1"))
    assert len(pub.sent) == 1
    subject, enriched = pub.sent[0]
    assert subject == "enriched.bluesky"
    assert isinstance(enriched, EnrichedItem)
    assert len(enriched.embedding) == EMBED_DIM
    assert inf.published == 1


async def test_duplicate_is_not_published() -> None:
    pub = _CapturePublisher()
    inf = Inference(CheapFilter(), pub)  # type: ignore[arg-type]
    item = _social("same text", "h2")
    await inf.handle(item)
    await inf.handle(item)
    assert len(pub.sent) == 1  # second sighting deduped before embed/publish
