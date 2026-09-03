"""Fitting a recorded series: slope, significance, anomaly.

The arithmetic only. Nothing here touches the database, the clock or the
config, which is what lets every claim be checked against a literal — and
these are claims that fail silently if they are wrong. A variance term
with a typo in it does not raise; it reports a confident direction in
noise, on every scan, forever. So two of the tests below are calibration
runs against seeded pseudo-random data: they are the only thing that
would notice.
"""

from __future__ import annotations

import random

import pytest

from vibe_sentinel import trends
from vibe_sentinel.schemas import TrendPoint


def _points(values: list[float], start: int = 1) -> list[TrendPoint]:
    return [
        TrendPoint(run_id=start + i, at=f"2026-08-{start + i:02d}", value=v, label="k")
        for i, v in enumerate(values)
    ]


def _noise(n: int, seed: int, sd: float = 1.0, slope: float = 0.0) -> list[float]:
    r = random.Random(seed)
    return [100 + slope * i + r.gauss(0, sd) for i in range(n)]


# --- the slope -------------------------------------------------------------


def test_theil_sen_is_exact_on_a_line() -> None:
    xs = [float(i) for i in range(10)]
    slope, intercept = trends.theil_sen(xs, [3 * x + 7 for x in xs])
    assert (slope, intercept) == (3.0, 7.0)


def test_theil_sen_survives_the_point_least_squares_does_not() -> None:
    """The contaminating point here is not noise — it is the one
    deliberate refactor in an otherwise steady series, and a line dragged
    through it reports a trend that reversed."""
    xs = [float(i) for i in range(10)]
    ys = [3 * x + 7 for x in xs]
    ys[5] = 900.0

    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    least_squares = sum(
        (x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)
    ) / sum((x - mean_x) ** 2 for x in xs)

    slope, _ = trends.theil_sen(xs, ys)
    assert slope == pytest.approx(3.0)
    assert least_squares > 8  # what the robust estimator is buying


def test_a_single_point_has_no_slope_to_estimate() -> None:
    assert trends.theil_sen([1.0], [5.0]) == (0.0, 5.0)


# --- the significance test -------------------------------------------------


def test_mann_kendall_matches_the_hand_computation() -> None:
    """n=10 strictly increasing: S = 45, Var = 10*9*25/18 = 125,
    z = 44/sqrt(125) = 3.9355, p = 2*Phi(-z)."""
    s, tau, p = trends.mann_kendall([float(i) for i in range(10)])
    assert s == 45.0
    assert tau == pytest.approx(1.0)
    assert p == pytest.approx(8.303e-05, rel=1e-3)


def test_the_test_is_symmetric_in_direction() -> None:
    rising = trends.mann_kendall([float(i) for i in range(10)])
    falling = trends.mann_kendall([float(-i) for i in range(10)])
    assert falling[0] == -rising[0]
    assert falling[1] == pytest.approx(-rising[1])
    assert falling[2] == pytest.approx(rising[2])


def test_a_constant_series_is_certain_rather_than_unknown() -> None:
    """Variance is zero here, and the answer is "no trend" — not a
    division, and not a trend whose p-value happens to be missing."""
    assert trends.mann_kendall([4.0] * 10) == (0.0, 0.0, 1.0)


def test_ties_are_corrected_for_rather_than_ignored() -> None:
    """An observation that sat at one value for most of its history is
    mostly ties, and counting them as information would understate the
    variance and call the trend significant."""
    tied = [1.0, 1.0, 1.0, 1.0, 1.0, 2.0, 2.0, 2.0, 2.0, 2.0]
    untied = [float(i) for i in range(10)]
    assert trends.mann_kendall(tied)[2] > trends.mann_kendall(untied)[2]


def test_too_short_to_test_is_not_a_trend() -> None:
    assert trends.mann_kendall([1.0, 2.0]) == (0.0, 0.0, 1.0)


def test_the_test_holds_its_nominal_rate_on_trendless_data() -> None:
    """The one test that would notice a wrong variance term.

    A typo there does not raise — it reports a confident direction in
    noise, on every scan. Seeded, so this is deterministic; the band is
    wide because 1500 trials is not 40000, and the estimator is
    deliberately conservative besides.
    """
    called = sum(
        trends.mann_kendall(_noise(20, seed))[2] <= trends.SIGNIFICANCE
        for seed in range(1500)
    )
    assert 0.02 <= called / 1500 <= 0.09


def test_the_test_finds_a_trend_that_is_really_there() -> None:
    found = sum(
        trends.mann_kendall(_noise(20, seed, slope=0.3))[2] <= trends.SIGNIFICANCE
        for seed in range(200)
    )
    assert found == 200


# --- fitting a series ------------------------------------------------------


def test_a_series_too_short_is_refused_rather_than_fitted() -> None:
    """Three points make a line through anything, and a direction quoted
    from them is a claim the data cannot support."""
    assert trends.fit_series("p", "k", _points([1.0, 2.0, 3.0])) is None


def test_a_creeping_series_is_caught_while_every_step_is_small() -> None:
    """The whole reason this exists. No step here clears a tolerance of
    1, and the direction is unmistakable."""
    fit = trends.fit_series("p", "k", _points([4.0 + 0.4 * i for i in range(20)]))
    assert fit is not None
    assert fit.direction == "rising"
    assert fit.significant is True
    assert fit.slope == pytest.approx(0.4)
    assert fit.p_value < 0.001
    assert fit.fitted_change == pytest.approx(0.4 * 19)


def test_a_flat_series_is_flat_and_says_so() -> None:
    """Deterministic rather than seeded: at a nominal 5% some draw of
    noise is always going to trend, and the rate is what the calibration
    run below checks. This one is about the verdict, not the rate."""
    fit = trends.fit_series("p", "k", _points([10.0, 13.0, 9.0, 12.0, 11.0] * 5))
    assert fit is not None
    assert fit.direction == "flat"
    assert fit.significant is False


def test_direction_follows_the_test_and_not_the_endpoints() -> None:
    """A series that ends higher than it started has not necessarily
    risen; that is the comparison this replaces."""
    values = _noise(30, seed=7)
    values[-1] += 5.0
    fit = trends.fit_series("p", "k", _points(values))
    assert fit is not None
    assert fit.last_value > fit.first_value
    assert fit.direction == "flat"


# --- anomalies -------------------------------------------------------------


def test_an_anomaly_is_measured_against_the_trend_not_the_average() -> None:
    """In a directory that has grown for months every early value is far
    from the mean and none of them is a surprise."""
    values = [4.0 + 0.5 * i for i in range(25)]
    fit = trends.fit_series("p", "k", _points(values))
    assert fit is not None and fit.anomalies == []

    values[12] = 40.0
    fit = trends.fit_series("p", "k", _points(values))
    assert fit is not None
    assert [a.run_id for a in fit.anomalies] == [13]
    assert fit.anomalies[0].z > trends.ANOMALY_Z


def test_a_short_series_gets_a_direction_but_never_an_anomaly() -> None:
    """The scale an anomaly is measured in is the thing too few points
    cannot estimate — at ten runs the inherited 3.5 called one clean
    value in twelve a surprise."""
    values = [4.0 + 0.5 * i for i in range(trends.ANOMALY_MIN_RUNS - 1)]
    values[8] = 90.0
    fit = trends.fit_series("p", "k", _points(values))
    assert fit is not None
    assert fit.anomalies == []


def test_a_jump_in_an_otherwise_unmoving_series_is_still_found() -> None:
    """MAD is zero here — most of the series is one value — and a scale
    of zero would call the jump ordinary."""
    values = [12.0] * 24
    values[20] = 31.0
    fit = trends.fit_series("p", "k", _points(values))
    assert fit is not None
    assert [a.run_id for a in fit.anomalies] == [21]


def test_a_perfectly_constant_series_has_no_anomalies_and_no_error() -> None:
    fit = trends.fit_series("p", "k", _points([12.0] * 24))
    assert fit is not None
    assert fit.scale == 0.0
    assert fit.anomalies == []


def test_scoring_a_new_value_is_out_of_sample() -> None:
    """A point included in its own fit pulls the line towards itself and
    then reports how near it is to where it pulled it."""
    values = [4.0 + 0.5 * i + n for i, n in enumerate(_noise(25, seed=9, sd=0.3))]
    fit = trends.fit_series("p", "k", _points(values))
    assert fit is not None
    assert fit.scale > 0

    assert trends.score(fit, fit.intercept + fit.slope * (fit.last_run + 1)) is None

    off_trend = trends.score(fit, 400.0, at="2026-09-02")
    assert off_trend is not None
    assert off_trend.run_id is None  # not recorded yet; it has no run
    assert off_trend.z is not None and off_trend.z > trends.ANOMALY_Z


def test_a_series_that_never_moved_can_still_be_departed_from() -> None:
    """The arithmetic is the only thing that cannot see this one. Every
    residual is zero, so there is no spread to divide by — and returning
    "no anomaly" would mean an observation steady at 14 for thirty runs
    could become 40 unremarked, which is the most legible jump there is.
    """
    fit = trends.fit_series("p", "k", _points([14.0] * 30))
    assert fit is not None and fit.scale == 0.0

    assert trends.score(fit, 14.0) is None

    moved = trends.score(fit, 40.0)
    assert moved is not None
    assert moved.value == 40.0
    assert moved.expected == pytest.approx(14.0)
    # No score, because none exists — not a fabricated zero.
    assert moved.z is None


def test_nothing_is_scored_against_a_history_too_short_to_carry_it() -> None:
    values = [4.0 + 0.5 * i for i in range(trends.ANOMALY_MIN_RUNS - 1)]
    fit = trends.fit_series("p", "k", _points(values))
    assert fit is not None
    assert trends.score(fit, 900.0) is None


def test_clean_values_are_not_called_anomalies_at_the_measured_rate() -> None:
    """The other calibration run. 3.5 on twenty runs flagged one clean
    value in fifty, which on a real tree is three false calls a scan and
    a section nobody reads.
    """
    called = 0
    for seed in range(1500):
        values = _noise(21, seed)
        fit = trends.fit_series("p", "k", _points(values[:-1]))
        assert fit is not None
        called += trends.score(fit, values[-1]) is not None
    assert called / 1500 <= 0.01


def test_a_perfectly_linear_series_is_judged_by_its_own_step() -> None:
    """Its residuals are the last bits of a double, around 1e-16, and
    dividing by those turns the next ordinary value into a z-score of
    1e15. The only variation such a series has ever shown is its step, so
    that is what a departure is measured in: one step is not news, three
    hundred is."""
    fit = trends.fit_series("p", "k", _points([4.0 + i for i in range(25)]))
    assert fit is not None
    assert fit.scale == 0.0
    assert fit.slope == pytest.approx(1.0)

    assert trends.score(fit, 28.0) is None  # missed a step; ordinary
    assert trends.score(fit, 30.0) is None  # ran ahead by a few; ordinary

    jumped = trends.score(fit, 400.0)
    assert jumped is not None
    assert jumped.z is not None and jumped.z > trends.ANOMALY_Z


def test_only_a_series_that_never_moved_reports_without_a_score() -> None:
    """The z-less anomaly is for the series with no spread *and* no step.
    Anything with a slope has a scale, and inventing a wordy special case
    for it would hide a number that exists."""
    flat = trends.fit_series("p", "k", _points([14.0] * 25))
    assert flat is not None
    departed = trends.score(flat, 40.0)
    assert departed is not None and departed.z is None

    sloped = trends.fit_series("p", "k", _points([4.0 + i for i in range(25)]))
    assert sloped is not None
    jumped = trends.score(sloped, 400.0)
    assert jumped is not None and jumped.z is not None
