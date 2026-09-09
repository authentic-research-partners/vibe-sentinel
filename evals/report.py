"""Rendering a suite, and writing it down. Nothing here measures anything.

Two surfaces, the same split the tool's own report keeps: a terminal
rendering for a person, and a JSON file for whatever compares two runs
later. The renderer's one rule is the one ``DriftReport.analyzed``
enforces upstream — a tier that did not run must not look like a tier
that found nothing, so a suite without a model says so at the top and
again beside every judged measurement.
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.metrics import Counts, Matrix, Measurement, Suite

#: Failing cases printed per measurement before the list is cut. The
#: whole list is always in the JSON; this is what fits on a screen.
_SHOWN_FAILURES = 8


def _pct(value: float | None) -> str:
    """A rate, or ``n/a`` where there was no denominator to divide by."""
    return "n/a" if value is None else f"{value:.3f}"


def _counts_lines(counts: Counts) -> list[str]:
    return [
        f"      tp {counts.tp}  fn {counts.fn}  fp {counts.fp}  tn {counts.tn}"
        f"   ({counts.total} cases)",
        f"      recall {_pct(counts.recall)}   precision {_pct(counts.precision)}"
        f"   false-alarm {_pct(counts.false_alarm_rate)}   f1 {_pct(counts.f1)}",
    ]


def _matrix_lines(matrix: Matrix) -> list[str]:
    labels = matrix.labels()
    width = max([len(label) for label in labels] + [8])
    head = "      expected \\ observed".ljust(28) + "".join(
        label.rjust(width + 2) for label in labels
    )
    lines = [
        f"      accuracy {_pct(matrix.accuracy)} ({matrix.correct}/{matrix.total})",
        head,
    ]
    for expected in sorted(matrix.cells):
        row = matrix.cells[expected]
        cells = "".join(str(row.get(label, 0)).rjust(width + 2) for label in labels)
        lines.append(f"      {expected}".ljust(28) + cells)
    return lines


def _measurement_lines(measurement: Measurement, verbose: bool) -> list[str]:
    lines = [
        f"  {measurement.id}  [{measurement.tier}]",
        f"      {measurement.question}",
    ]
    if not measurement.ran:
        lines.append(f"      not measured — {measurement.note or 'no reason recorded'}")
        return lines
    if measurement.counts is not None:
        lines += _counts_lines(measurement.counts)
    if measurement.matrix is not None:
        lines += _matrix_lines(measurement.matrix)
    if measurement.counts is None and measurement.matrix is None:
        hits = sum(1 for o in measurement.outcomes if o.ok)
        lines.append(
            f"      on label {_pct(measurement.agreement)} "
            f"({hits}/{len(measurement.outcomes)})"
        )
    if measurement.unanswered:
        lines.append(
            f"      {measurement.unanswered} case(s) the model did not answer "
            f"about — not counted as wrong answers"
        )
    if measurement.note:
        lines.append(f"      note: {measurement.note}")
    failures = measurement.failures
    if failures:
        shown = failures if verbose else failures[:_SHOWN_FAILURES]
        lines.append(f"      {len(failures)} case(s) off their label:")
        for outcome in shown:
            detail = f" — {outcome.detail}" if outcome.detail else ""
            lines.append(
                f"        {outcome.case}: expected {outcome.expected}, "
                f"got {outcome.observed}{detail}"
            )
        if len(shown) < len(failures):
            lines.append(
                f"        ... {len(failures) - len(shown)} more (all of them "
                f"are in the JSON)"
            )
    return lines


def render(suite: Suite, verbose: bool = False) -> str:
    """One suite as text."""
    lines = [
        f"=== {suite.id} — {suite.title} ===",
        f"    {suite.rows} labelled outcome(s) across "
        f"{len(suite.measurements)} measurement(s), in {suite.duration_s:.1f}s",
    ]
    if suite.used_model:
        lines.append(f"    judged by {suite.model} at {suite.endpoint}")
    else:
        lines.append(
            f"    judged tier did not run — "
            f"{suite.note or 'no model was asked'}. Nothing below is a "
            f"statement about the model's answers."
        )
    lines.append("")
    for measurement in suite.measurements:
        lines += _measurement_lines(measurement, verbose)
        lines.append("")
    return "\n".join(lines)


def render_spread(
    rates: dict[str, tuple[str, list[tuple[float | None, int]]]],
) -> str:
    """Each measurement's headline number across the repeats, in order.

    With the denominator, always. A run the backend half-answered scores
    over what it answered, so a rate that rose while its denominator fell
    is an outage wearing an improvement's clothes.
    """
    if not rates:
        return ""
    lines = [
        "=== across the repeats ===",
        "    the one number each measurement is read by, run by run",
    ]
    for name, (label, values) in rates.items():
        rendered = ", ".join(f"{_pct(value)} of {n}" for value, n in values)
        lines.append(f"  {name}  {label}: {rendered}")
    return "\n".join(lines) + "\n"


def render_stability(rates: dict[str, float | None]) -> str:
    """The repeat-run agreement table, when more than one run happened."""
    if not rates:
        return ""
    lines = [
        "=== stability across repeats ===",
        "    how often identical requests came back with the same label",
    ]
    for name, rate in rates.items():
        lines.append(f"  {name}: {_pct(rate)}")
    return "\n".join(lines) + "\n"


def write(suite: Suite, output_dir: Path) -> Path:
    """The suite as JSON, at ``<output_dir>/<id>-eval.json``.

    Written every run, because the point of a rate is comparing it with
    the last one. Overwritten rather than appended: this is a result, not
    a history — the one artifact here that *is* a history has a database
    and rules of its own.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{suite.id}-eval.json"
    path.write_text(
        json.dumps(suite.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def as_json(suites: list[Suite]) -> str:
    """Every suite in one object, for ``--format json``."""
    return json.dumps(
        {suite.id: suite.model_dump(mode="json") for suite in suites},
        indent=2,
        sort_keys=True,
    )
