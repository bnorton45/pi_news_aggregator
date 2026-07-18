"""Claim-extraction worker tests (PLAN §3.3): fake LLM, no Ollama, no DB."""

from uuid import uuid4

from libs.schema import ClaimRequest, ClaimResult
from services.cluster.claimx import ClaimExtractor


class _FakeLlm:
    """Stands in for OllamaClient — returns a canned completion, records the call."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def chat(self, *, prompt: str, system: str | None = None, **kw) -> str:
        self.calls.append({"prompt": prompt, "system": system})
        return self.reply


class _CapturePublisher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, ClaimResult]] = []

    async def publish(self, subject: str, model: ClaimResult) -> None:
        self.sent.append((subject, model))


def _request(text: str) -> ClaimRequest:
    return ClaimRequest(story_id=uuid4(), item_id=uuid4(), text=text)


async def test_extracted_claim_is_published_with_story_and_item_ids() -> None:
    llm = _FakeLlm("  A magnitude 6 quake struck near Tokyo.\n")
    pub = _CapturePublisher()
    x = ClaimExtractor(llm, pub)  # type: ignore[arg-type]
    req = _request("BREAKING!!! huge quake tokyo omg")
    await x.handle(req)
    assert len(pub.sent) == 1
    subject, result = pub.sent[0]
    assert subject == "claim.extracted"
    assert result.story_id == req.story_id
    assert result.item_id == req.item_id
    assert result.claim == "A magnitude 6 quake struck near Tokyo."  # stripped
    assert llm.calls[0]["prompt"] == req.text
    assert x.extracted == 1


async def test_no_checkable_claim_publishes_empty_claim() -> None:
    llm = _FakeLlm("\n")  # model's "no factual claim" convention
    pub = _CapturePublisher()
    x = ClaimExtractor(llm, pub)  # type: ignore[arg-type]
    await x.handle(_request("i love mondays"))
    assert pub.sent[0][1].claim == ""


async def test_overlong_model_output_is_truncated_to_schema_cap() -> None:
    llm = _FakeLlm("x" * 5000)
    pub = _CapturePublisher()
    x = ClaimExtractor(llm, pub)  # type: ignore[arg-type]
    await x.handle(_request("t"))
    assert len(pub.sent[0][1].claim) == 2048  # ClaimResult max_length holds
