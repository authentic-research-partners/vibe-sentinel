"""How far back a comparison reaches.

The baseline is one point in time, and one point shows drift at one
scale. A directory gaining two modules a week is under every tolerance
between consecutive scans and a reorganisation by Christmas — and which
scale a given codebase's drift shows up at is not something anyone knows
in advance. A horizon (``1w``, ``1m``) names a second point to compare
against, and this module is the arithmetic that turns the declared string
into a timestamp the run selector can look for.

A leaf, deliberately: it imports nothing from this package. ``config.py``
validates a declared horizon when the file loads, and ``config`` is
imported by nearly every command — a validator that dragged the diff
engine in behind it would be an inverted dependency, and the tool caught
exactly that when these three functions lived in :mod:`inventory`.
Same reasoning as :mod:`vibe_sentinel.paths`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

#: What each unit suffix is worth. Months and years are the plain
#: approximations, not calendar arithmetic: a horizon is a rough arc over
#: which structure is expected to hold, and no answer changes because
#: February is short.
UNITS: dict[str, timedelta] = {
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
    "mo": timedelta(days=30),
    "m": timedelta(days=30),
    "y": timedelta(days=365),
}

_HORIZON_RE = re.compile(r"^(\d+)(mo|[hdwmy])$")


def parse_horizon(text: str) -> timedelta:
    """Turn a declared horizon like ``1w`` into a length of time.

    Minutes are deliberately absent, and that absence is what lets ``m``
    mean *month* without ambiguity: a horizon shorter than an hour is one
    over which nothing structural happens, so the reading that would have
    made ``m`` ambiguous is a reading nobody wants.
    """
    match = _HORIZON_RE.match(text.strip().lower())
    if match is None:
        raise ValueError(
            f"{text!r} is not a horizon. Write a whole number and a unit — "
            "h (hours), d (days), w (weeks), m or mo (30 days), y (365 days) "
            "— as in '1w' or '3m'."
        )
    count = int(match.group(1))
    if count == 0:
        raise ValueError(
            f"{text!r} is a horizon of no length, which compares a run "
            "against itself. Name a real one, as in '1w'."
        )
    return count * UNITS[match.group(2)]


def horizon_cutoff(horizon: str, now: datetime | None = None) -> str:
    """The moment one horizon ago, formatted as ``runs.started_at`` stores it.

    Seconds precision and an explicit UTC offset, so the string compares
    lexicographically against the column — which is what lets the run
    selector use the index on it rather than parsing every row.
    """
    moment = (now or datetime.now(UTC)) - parse_horizon(horizon)
    return moment.isoformat(timespec="seconds")


def age_days(at: str, now: datetime | None = None) -> float:
    """How long ago ``at`` was, in days.

    Reported next to every horizon because the run one selects is the
    newest one *at least* that old, which can be considerably older. A
    comparison labelled ``1w`` that actually reached back eleven days
    should say eleven.
    """
    try:
        then = datetime.fromisoformat(at)
    except ValueError:
        # A timestamp this cannot read is one the database should not
        # hold. Say "no age" rather than raising: the horizon's own
        # finding is still worth printing, and a renderer is the wrong
        # place to discover a malformed row.
        return 0.0
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - then).total_seconds() / 86400.0
