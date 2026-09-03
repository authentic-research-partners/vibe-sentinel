"""Fitting the recorded history: is this moving, and did this jump?

A baseline comparison answers "did it move since last time". A horizon
answers "did it move since a month ago". Neither answers the question the
history is actually able to settle — *has this been going one way for
weeks* — because both compare two points and a direction is a property of
all of them. Two modules a week clears no tolerance and is a
reorganisation by Christmas; the horizon catches it once it is large, and
a slope catches it while it is still small.

Three closed-form estimators, in stdlib arithmetic, and the choice is
deliberate:

- **Theil–Sen** for the slope — the median of the pairwise slopes, not
  least squares. One refactor that halves a directory would drag a
  least-squares line through a series that never trended.
- **Mann–Kendall** for whether the slope means anything. Nonparametric,
  so it assumes nothing about how these values are distributed, which is
  as well: nobody knows how "modules per directory" is distributed.
- **MAD on the residuals** for anomalies. Detrended first, because in a
  steadily growing series every early point is far from the mean and none
  of them is a surprise. What is a surprise is a point far from the
  *trend*.

No dependency, no training, no state. That is not a compromise, it is the
requirement: an estimator that had to be fitted and stored would be a
second thing to keep in step with the history, and one that answered
differently on identical input would manufacture drift — the failure this
project already had when a model chose probe parameters.

Nothing here reads the database, the clock, or the config. It takes a
series and returns a fit, which is what makes every claim below testable
against a literal.
"""

from __future__ import annotations

from collections import Counter
from math import isclose, sqrt
from statistics import NormalDist, fmean, median

from vibe_sentinel.schemas import Anomaly, TrendFit, TrendPoint

#: Runs a series needs before its direction is quoted. Measured against
#: 40,000 trendless series per row, at a nominal 5%::
#:
#:     n:       5      6      8     10     12     15     20     30
#:     called:  1.7%   1.6%   3.2%   4.7%   4.4%   4.6%   4.5%   4.7%
#:
#: Ten is where the normal approximation becomes calibrated. Below it the
#: test is *conservative* rather than wrong — it under-reports, so nothing
#: false is claimed and there is simply less power than the p-value
#: suggests — but a direction quoted from five points is a claim the data
#: cannot support, and refusing is the cheaper mistake.
MIN_RUNS = 10

#: Two-sided p at or under which a direction is called. The conventional
#: 0.05, chosen for the conventional reason and stated rather than tuned:
#: a threshold moved until the findings look right is not a threshold.
SIGNIFICANCE = 0.05

#: Runs a series needs before any point is called an anomaly, and the
#: modified z-score that does it.
#:
#: Iglewicz and Hoaglin's 3.5 is the usual recommendation and it is wrong
#: here, because it assumes a scale you know rather than one estimated
#: from a handful of points. Measured out of sample — fit on n runs,
#: score the next clean value, 20,000 trials — as the share of clean
#: values called anomalous, and beside it what that costs per scan on
#: this repository's 180 observations::
#:
#:     n     |z|>=3.5        |z|>=4.5        |z|>=5.0
#:     10    8.40%  (15.1)   4.58%  (8.2)    3.39%  (6.1)
#:     20    1.97%  (3.6)    0.57%  (1.0)    0.39%  (0.7)
#:     30    0.88%  (1.6)    0.14%  (0.3)    0.06%  (0.1)
#:     50    0.47%  (0.8)    0.10%  (0.2)    0.03%  (0.0)
#:
#: Fifteen false anomalies a scan is not a signal, it is why a section
#: gets skipped. So: twenty runs before the question is asked at all, and
#: 5.0 rather than 3.5 — under one expected false call per scan at the
#: floor, and roughly one per twenty-five scans once a history is fifty
#: runs deep. It costs power on small jumps (a 3-sigma step is caught
#: 12% of the time, a 5-sigma one 57%, an 8-sigma one 98%), which is the
#: right trade for something reported unprompted on every scan: a
#: structural jump worth a person's attention is a directory doubling,
#: not a module either way.
ANOMALY_MIN_RUNS = 20
ANOMALY_Z = 5.0

#: Scale factors turning a median (or mean) absolute deviation into
#: something comparable to a standard deviation for normal data.
_MAD_SCALE = 0.6745
_MEAN_AD_SCALE = 1.253314


def theil_sen(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Slope and intercept by medians rather than by least squares.

    The median of every pairwise slope, which tolerates up to about 29%
    of the points being anything at all. That matters here because the
    contaminating point is not noise — it is the one deliberate refactor
    in an otherwise steady series, and a line dragged through it would
    report a trend that reversed.
    """
    slopes = [
        (ys[j] - ys[i]) / (xs[j] - xs[i])
        for i in range(len(xs))
        for j in range(i + 1, len(xs))
        if xs[j] != xs[i]
    ]
    if not slopes:
        return 0.0, median(ys) if ys else 0.0
    slope = median(slopes)
    return slope, median(y - slope * x for x, y in zip(xs, ys, strict=True))


def mann_kendall(ys: list[float]) -> tuple[float, float, float]:
    """Whether a series trends. Returns ``(S, tau, p)``.

    Counts how many later values exceed earlier ones against how many
    fall short; under "no trend" that difference is centred on zero with
    a known variance, so the whole test is a z-score. It uses only the
    order of the values, never their spacing, which is what makes it
    indifferent to a probe whose numbers are a ratio, a count, or a line
    total.

    Ties are corrected for rather than ignored: an observation that sat
    at 12 for thirty runs is mostly ties, and pretending otherwise would
    understate the variance and call the trend significant.
    """
    n = len(ys)
    if n < 3:
        return 0.0, 0.0, 1.0

    s = 0
    for i in range(n):
        for j in range(i + 1, n):
            delta = ys[j] - ys[i]
            s += (delta > 0) - (delta < 0)

    groups = [t for t in Counter(ys).values() if t > 1]
    tie_term = sum(t * (t - 1) * (2 * t + 5) for t in groups)
    variance = (n * (n - 1) * (2 * n + 5) - tie_term) / 18.0
    if variance <= 0:
        # Every value identical. No trend, and nothing to be uncertain
        # about — not a trend whose p-value happens to be missing.
        return float(s), 0.0, 1.0

    # The continuity correction: S is integer-valued and the normal
    # approximation is not, so the step towards zero is what keeps a
    # marginal series from reading as significant.
    if s > 0:
        z = (s - 1) / sqrt(variance)
    elif s < 0:
        z = (s + 1) / sqrt(variance)
    else:
        z = 0.0
    # cdf(-|z|) rather than 1 - cdf(|z|): the same number, computed in
    # the tail where the floats still have digits.
    p = 2.0 * NormalDist().cdf(-abs(z))

    pairs = n * (n - 1) / 2.0
    tied_pairs = sum(t * (t - 1) / 2.0 for t in groups)
    denominator = sqrt((pairs - tied_pairs) * pairs)
    tau = s / denominator if denominator > 0 else 0.0
    return float(s), tau, min(1.0, max(0.0, p))


#: Below this fraction of the values' own magnitude, a spread is the
#: floating-point arithmetic rather than anything the codebase did.
_NOISE_FLOOR = 1e-9


def _spread(residuals: list[float], magnitude: float) -> float:
    """A robust scale for the residuals, comparable to a deviation.

    The median absolute deviation, except where it is zero — which
    happens exactly when most of the series sits on one value, and is
    precisely the series where a single jump matters most. A scale of
    zero would either divide by nothing or declare the jump ordinary, so
    the mean absolute deviation stands in, on Iglewicz and Hoaglin's
    alternative constant.

    And zero is not only reached exactly. A directory that gained one
    module per run for thirty runs fits a line it sits on, and the
    residuals are then the last bits of the doubles rather than anything
    the codebase did — around 1e-16. Dividing by that turns the next
    ordinary value into a z-score of 1e15. So a spread under
    ``_NOISE_FLOOR`` of the values' own magnitude is reported as no
    spread at all, which is what it is, and the caller says so in words.
    """
    if not residuals:
        return 0.0
    centre = median(residuals)
    floor = _NOISE_FLOOR * max(abs(magnitude), 1.0)
    mad = median(abs(r - centre) for r in residuals)
    if mad > floor:
        return mad / _MAD_SCALE
    mean_ad = fmean(abs(r - centre) for r in residuals)
    return _MEAN_AD_SCALE * mean_ad if mean_ad > floor else 0.0


def fit_series(
    probe_id: str,
    key: str,
    points: list[TrendPoint],
    min_runs: int = MIN_RUNS,
) -> TrendFit | None:
    """Fit one observation's history. None when there is not enough of it.

    Refusing under ``min_runs`` rather than fitting anyway: three points
    make a line through anything, and a direction quoted from them is a
    claim the data cannot support.
    """
    usable = [p for p in points if p.value is not None]
    if len(usable) < max(3, min_runs):
        return None

    xs = [float(p.run_id) for p in usable]
    ys = [float(p.value) for p in usable if p.value is not None]
    slope, intercept = theil_sen(xs, ys)
    _, tau, p_value = mann_kendall(ys)

    significant = p_value <= SIGNIFICANCE and slope != 0.0
    if not significant:
        direction = "flat"
    else:
        direction = "rising" if slope > 0 else "falling"

    residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys, strict=True)]
    scale = _spread(residuals, magnitude=median(ys))
    centre = median(residuals)
    anomalies: list[Anomaly] = []
    # A shorter series still gets a fit and a direction; what it does not
    # get is an anomaly, because the scale those are measured in is the
    # thing too few points cannot estimate. See ANOMALY_MIN_RUNS.
    if scale > 0 and len(usable) >= ANOMALY_MIN_RUNS:
        for point, y, x, residual in zip(usable, ys, xs, residuals, strict=True):
            z = (residual - centre) / scale
            if abs(z) >= ANOMALY_Z:
                anomalies.append(
                    Anomaly(
                        run_id=point.run_id,
                        at=point.at,
                        value=y,
                        expected=intercept + slope * x,
                        z=z,
                    )
                )

    span = xs[-1] - xs[0]
    return TrendFit(
        probe_id=probe_id,
        key=key,
        label=usable[-1].label,
        runs=len(usable),
        first_run=usable[0].run_id,
        last_run=usable[-1].run_id,
        first_value=ys[0],
        last_value=ys[-1],
        slope=slope,
        intercept=intercept,
        fitted_change=slope * span,
        tau=tau,
        p_value=p_value,
        significant=significant,
        direction=direction,  # type: ignore[arg-type]
        scale=scale,
        anomalies=anomalies,
    )


def score(fit: TrendFit, value: float, at: str = "") -> Anomaly | None:
    """Where a newly measured value falls against a fit made without it.

    Out of sample, deliberately. A point included in its own fit pulls
    the line towards itself and then reports how close it is to where it
    pulled it — which is how an anomaly detector comes to find nothing.
    The scan fits the history it has recorded and scores the value it
    just measured against that.
    """
    if fit.runs < ANOMALY_MIN_RUNS:
        return None

    # The next run has no id yet — it is assigned when this scan is
    # recorded, after every reading here is computed — so the fit is
    # extended by one. Runs are dense unless a prune took some out of the
    # middle, and one run's worth of slope is not what decides this.
    expected = fit.intercept + fit.slope * (fit.last_run + 1)

    scale = fit.scale
    if scale <= 0:
        # No residual spread at all: the series sat exactly on its own
        # fit. There is still a scale to judge by, and it is the only
        # variation the series has ever shown — its own step. A directory
        # that gained one module every run for thirty runs and gained
        # none this run has moved by one step, which is not news; the
        # same series arriving at 400 has moved by three hundred, which
        # is. Without this the first case scores 1e15, because the
        # residuals being divided by are the last bits of a double.
        if fit.slope != 0:
            scale = abs(fit.slope)
        else:
            # A series that held one value on every run has no step
            # either, and "no score" is not "nothing happened" — this is
            # the most legible anomaly there is and the arithmetic is the
            # only thing that cannot see it. Reported without a z,
            # because none exists, and said in words instead.
            if isclose(value, expected, rel_tol=1e-9, abs_tol=1e-12):
                return None
            return Anomaly(run_id=None, at=at, value=value, expected=expected, z=None)

    z = (value - expected) / scale
    if abs(z) < ANOMALY_Z:
        return None
    return Anomaly(run_id=None, at=at, value=value, expected=expected, z=z)
