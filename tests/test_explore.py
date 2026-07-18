"""§6.3a exploration quota + weak-label capture plumbing."""

from libs.schema import EnrichedItem, Item, SourceClass
from services.enrich.filter import CheapFilter
from services.enrich.governor import AdmissionGovernor, GovernorConfig
from services.enrich.main import Inference


class _CapturePublisher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, EnrichedItem]] = []

    async def publish(self, subject: str, model: EnrichedItem) -> None:
        self.sent.append((subject, model))


def _social(text: str, content_hash: str) -> Item:
    return Item(
        source="bluesky", source_class=SourceClass.SOCIAL, text=text, content_hash=content_hash
    )


def test_explore_always_off_at_ceiling() -> None:
    gov = AdmissionGovernor(GovernorConfig(explore_rate=1.0))
    gov.theta = gov.cfg.theta_ceiling  # sustained over-budget: no spare budget
    assert gov.at_ceiling
    assert not any(gov.explore() for _ in range(100))


def test_explore_rate_extremes() -> None:
    always = AdmissionGovernor(GovernorConfig(explore_rate=1.0))
    always.theta = 0.5  # sampling, below ceiling
    assert all(always.explore() for _ in range(100))
    never = AdmissionGovernor(GovernorConfig(explore_rate=0.0))
    never.theta = 0.5
    assert not any(never.explore() for _ in range(100))


async def test_shed_item_can_be_explored_and_is_tagged() -> None:
    pub = _CapturePublisher()
    inf = Inference(CheapFilter(), pub)  # type: ignore[arg-type]
    inf.gov.theta = 0.99  # force the gate shut for a bland item
    inf.gov.explore = lambda: True  # type: ignore[method-assign] # deterministic sample
    await inf.handle(_social("nothing much happening", "h-explore"))
    assert len(pub.sent) == 1
    assert pub.sent[0][1].exploration is True
    assert inf.explored == 1 and inf.published == 1 and inf.shed == 0


async def test_shed_item_without_exploration_is_dropped() -> None:
    pub = _CapturePublisher()
    inf = Inference(CheapFilter(), pub)  # type: ignore[arg-type]
    inf.gov.theta = 0.99
    inf.gov.explore = lambda: False  # type: ignore[method-assign]
    await inf.handle(_social("nothing much happening", "h-shed"))
    assert pub.sent == []
    assert inf.shed == 1 and inf.explored == 0


def test_enriched_roundtrip_keeps_exploration_flag() -> None:
    from libs.schema import EMBED_DIM

    e = EnrichedItem(item=_social("x", "h1"), embedding=[0.0] * EMBED_DIM, exploration=True)
    assert EnrichedItem.model_validate_json(e.model_dump_json()).exploration is True
    d = EnrichedItem(item=_social("x", "h2"), embedding=[0.0] * EMBED_DIM)
    assert d.exploration is False  # default: a real admission
