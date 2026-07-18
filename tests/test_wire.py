"""Wire-attribution detection tests (PLAN §6.5 syndication collapse).

Precision posture under test: full org names match anywhere (misses overcount
origins → unsafe), terse tags only in the dateline window, bare 'AP' never.
"""

from libs.trust import wire_ref

# ── dateline tags ─────────────────────────────────────────────────────────────


def test_ap_dateline() -> None:
    assert wire_ref("WICHITA, Kan. (AP) — Severe storms tore through the county.") == "ap"


def test_reuters_dateline() -> None:
    assert wire_ref("LONDON (Reuters) - The central bank held rates steady.") == "reuters"


def test_afp_dateline() -> None:
    assert wire_ref("PARIS (AFP) — Protests continued for a third day.") == "afp"


def test_gray_dateline() -> None:
    assert wire_ref("(Gray News) - A recall was issued for the product.") == "gray"


def test_tag_outside_dateline_window_ignored() -> None:
    filler = "a" * 200
    assert wire_ref(f"{filler} (AP) something") == ""


# ── credit names, anywhere ────────────────────────────────────────────────────


def test_ap_credit_line() -> None:
    text = "Crews searched overnight. The Associated Press contributed to this report."
    assert wire_ref(text) == "ap"


def test_reuters_body_mention() -> None:
    # Recall-first: a body mention folds the item into the wire origin — the
    # trust-conservative direction (undercounts origins, never forges them).
    assert wire_ref("Officials confirmed the toll, Reuters reported.") == "reuters"


def test_cnn_newsource_credit() -> None:
    assert wire_ref("Video provided by CNN Newsource.") == "cnn"


def test_dateline_beats_body_credit() -> None:
    text = "DENVER (AP) — The outage spread. Reuters reported a similar event abroad."
    assert wire_ref(text) == "ap"


# ── negatives ─────────────────────────────────────────────────────────────────


def test_bare_ap_initials_never_match() -> None:
    assert wire_ref("AP Calculus scores rose at AP Highschool this year.") == ""
    assert wire_ref("The APT group targeted routers. (APT41)") == ""


def test_lowercase_tag_ignored() -> None:
    assert wire_ref("somewhere (ap) - not a wire mark") == ""


def test_plain_local_report() -> None:
    assert wire_ref("City council approved the annex rezoning 5-2 on Tuesday night.") == ""


def test_empty() -> None:
    assert wire_ref("") == ""
