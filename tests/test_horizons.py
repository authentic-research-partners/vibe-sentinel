"""How far back a comparison reaches.

The arithmetic only — a horizon names a point in time, and nothing here
knows what is compared at it. Which is the whole reason this is a leaf
module: ``config.py`` validates a declared horizon when the file loads,
and it must not drag the diff engine in behind it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vibe_sentinel.horizons import age_days, horizon_cutoff, parse_horizon
from vibe_sentinel.schemas import Snapshot


def test_every_unit_parses() -> None:
    assert parse_horizon("6h") == timedelta(hours=6)
    assert parse_horizon("3d") == timedelta(days=3)
    assert parse_horizon("2w") == timedelta(days=14)
    assert parse_horizon("1y") == timedelta(days=365)


def test_m_means_month_because_minutes_do_not_exist() -> None:
    """The ambiguity is removed by construction rather than by a rule.

    A horizon shorter than an hour is one over which nothing structural
    happens, so the reading that would have made ``m`` ambiguous is a
    reading nobody wants — and ``mo`` stays for anyone who distrusts it.
    """
    assert parse_horizon("1m") == timedelta(days=30)
    assert parse_horizon("1mo") == parse_horizon("1m")
    with pytest.raises(ValueError):
        parse_horizon("30min")


@pytest.mark.parametrize("bad", ["", "7", "w", "1.5w", "-1d", "week", "1 w x"])
def test_a_horizon_that_is_not_one_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_horizon(bad)


def test_a_horizon_of_no_length_is_refused() -> None:
    """It would compare a run against itself and report that as calm."""
    with pytest.raises(ValueError, match="no length"):
        parse_horizon("0d")


def test_the_error_names_the_units_it_would_have_taken() -> None:
    with pytest.raises(ValueError, match="h .hours."):
        parse_horizon("1fortnight")


def test_a_horizon_is_case_and_space_insensitive() -> None:
    assert parse_horizon("  2W  ") == parse_horizon("2w")


def test_cutoff_is_shaped_like_the_column_it_is_compared_against() -> None:
    """Lexicographic comparison against ``runs.started_at`` is what lets
    the selector use the index rather than parsing every row, and that
    only holds while both carry seconds and the same offset."""
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    assert horizon_cutoff("1w", now) == "2026-08-26T12:00:00+00:00"
    assert horizon_cutoff("1w", now) < Snapshot(generated_at="2026-09-01").generated_at


def test_age_is_reported_from_the_run_actually_selected() -> None:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    assert age_days("2026-08-22T12:00:00+00:00", now) == 11.0


def test_an_unreadable_timestamp_costs_the_age_and_not_the_finding() -> None:
    """A renderer is the wrong place to discover a malformed row."""
    assert age_days("not a time") == 0.0
