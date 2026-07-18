"""Read-API query-selection tests (services.api.db).

Pure string assertions on the SQL the feed builds — no DB needed. The actual row
ordering is exercised against a live Postgres in the compose/e2e loop.
"""

from services.api.db import STORY_QUERIES


def test_sort_modes() -> None:
    assert set(STORY_QUERIES) == {"gap", "corroborated", "primary", "weather"}


def test_gap_feed_ranks_by_gap_and_keeps_all_trust_levels() -> None:
    q = STORY_QUERIES["gap"]
    assert "ORDER BY s.gap DESC" in q
    assert "AND s.trust_state" not in q  # gap feed does not filter by trust
    assert "NOT (s.source_set ? 'noaa')" in q  # weather routed to its own tab


def test_corroborated_watchlist_is_exactly_corroborated() -> None:
    q = STORY_QUERIES["corroborated"]
    # exactly corroborated — primary-backed has moved to its own tab
    assert "s.trust_state = 'corroborated'" in q
    assert "primary_backed" not in q
    assert "NOT (s.source_set ? 'noaa')" in q  # no weather here either
    # still windowed + bounded like the default feed
    assert "now() - $1::interval" in q
    assert "LIMIT $2" in q


def test_primary_watchlist_is_exactly_primary_backed() -> None:
    q = STORY_QUERIES["primary"]
    assert "s.trust_state = 'primary_backed'" in q
    assert "NOT (s.source_set ? 'noaa')" in q
    assert "ORDER BY s.gap DESC" in q
    assert "LIMIT $2" in q


def test_weather_tab_is_the_noaa_complement() -> None:
    q = STORY_QUERIES["weather"]
    assert "s.source_set ? 'noaa'" in q
    assert "NOT (s.source_set ? 'noaa')" not in q  # this IS the noaa view
    assert "AND s.trust_state" not in q  # weather spans every trust level
    assert "ORDER BY s.gap DESC" in q
    assert "LIMIT $2" in q
