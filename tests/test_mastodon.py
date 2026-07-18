"""Mastodon normalize/should_keep tests — fixtures mirror /api/v1/timelines/public."""

from services.ingest.mastodon.normalize import normalize, should_keep, strip_html

STATUS = {
    "id": "116858726662338199",
    "created_at": "2026-07-03T23:50:59.000Z",
    "in_reply_to_id": None,
    "language": "en",
    "uri": "https://mastodon.social/ap/users/116180841132480601/statuses/116858726600376682",
    "url": "https://mastodon.social/@someone/116858726600376682",
    "visibility": "public",
    "sensitive": False,
    "content": "<p>Quake felt in Guatemala City &amp; suburbs</p><p>Details emerging</p>",
    "account": {"id": "116245180360844124", "acct": "someone@mastodon.social", "bot": False},
    "reblog": None,
}


def test_strip_html() -> None:
    assert (
        strip_html("<p>Quake felt &amp; reported</p><p>More<br>soon</p>")
        == "Quake felt & reported\nMore\nsoon"
    )


def test_keep_public_en_original() -> None:
    assert should_keep(STATUS) is True


def test_shaping_drops_noise() -> None:
    assert should_keep({**STATUS, "reblog": {"id": "1"}}) is False
    assert should_keep({**STATUS, "visibility": "unlisted"}) is False
    assert should_keep({**STATUS, "language": "ja"}) is False
    assert should_keep({**STATUS, "language": None}) is False
    assert should_keep({**STATUS, "account": {**STATUS["account"], "bot": True}}) is False
    assert (
        should_keep({**STATUS, "account": {**STATUS["account"], "bot": True}}, skip_bots=False)
        is True
    )
    assert should_keep({**STATUS, "content": "<p>   </p>"}) is False


def test_normalize_status() -> None:
    item = normalize(STATUS, "mstdn.social")
    assert item is not None
    assert item.source == "mastodon"
    assert item.source_class.value == "social"
    assert item.text == "Quake felt in Guatemala City & suburbs\nDetails emerging"
    assert item.lang == "en"
    assert item.urls == ["https://mastodon.social/@someone/116858726600376682"]
    assert item.ts_event is not None and item.ts_event.tzinfo is not None
    assert item.raw_ref == "116858726662338199"


def test_identity_is_hashed_not_raw() -> None:
    item = normalize(STATUS, "mstdn.social")
    assert item is not None
    assert "116245180360844124" not in item.author_ref
    assert "someone" not in item.author_ref
    assert item.content_hash.startswith("masto:")


def test_federated_copies_share_content_hash() -> None:
    """The same status seen from two instances keys on the same ActivityPub uri."""
    a = normalize(STATUS, "mstdn.social")
    b = normalize(STATUS, "mas.to")
    assert a is not None and b is not None
    assert a.content_hash == b.content_hash
    assert a.author_ref != b.author_ref  # instance-scoped account hash is fine


def test_unusable_status_dropped() -> None:
    assert normalize({}, "mstdn.social") is None
    assert normalize({**STATUS, "uri": ""}, "mstdn.social") is None
    assert normalize({**STATUS, "content": "<p></p>"}, "mstdn.social") is None
