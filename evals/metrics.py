"""The arithmetic, and the shape of a result. Nothing here runs or prints.

Two tables, because the questions have two shapes. A stage that either
fires or does not — triage, the credential pattern match, the near-miss
pairing — is a :class:`Counts`, and what matters about it is the pair of
error rates that pull in opposite directions: a miss is the whole
failure, and a false alarm is how a gate gets switched off. A stage that
picks one of several labels — ``safe`` / ``unclear`` / ``unsafe``,
``real`` / ``placeholder`` / ``unclear`` — is a :class:`Matrix`, because
the interesting part is *which* wrong answer it gave.

**An undefined rate is None, never zero.** Precision over no predictions
at all is not 0.0, and a renderer that prints it as one has invented a
measurement. Same rule the report keeps for a horizon that reached no
run: not measured and measured-clean are different states and must not
render alike.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Tier = Literal["mechanical", "judged"]


def _ratio(numerator: int, denominator: int) -> float | None:
    """``numerator / denominator``, or None when there is no denominator."""
    return numerator / denominator if denominator else None


class Counts(BaseModel):
    """One two-way confusion table: did the stage fire when it should?"""

    tp: int = 0
    """Should have fired, did."""
    fp: int = 0
    """Should not have fired, did — the rate that gets a gate switched off."""
    fn: int = 0
    """Should have fired, did not — the failure the gate exists to prevent."""
    tn: int = 0
    """Should not have fired, did not."""

    def record(self, expected: bool, observed: bool) -> None:
        if expected and observed:
            self.tp += 1
        elif expected and not observed:
            self.fn += 1
        elif observed:
            self.fp += 1
        else:
            self.tn += 1

    @property
    def total(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    @property
    def precision(self) -> float | None:
        return _ratio(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float | None:
        return _ratio(self.tp, self.tp + self.fn)

    @property
    def false_alarm_rate(self) -> float | None:
        """Of the cases that must not fire, how many did.

        Reported beside precision rather than folded into it because it is
        the number with a published bar: CARE puts the ceiling for a gate
        people leave switched on at under 1%, and precision moves with the
        corpus mix while this does not.
        """
        return _ratio(self.fp, self.fp + self.tn)

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or p + r == 0:
            return None
        return 2 * p * r / (p + r)

    @property
    def accuracy(self) -> float | None:
        return _ratio(self.tp + self.tn, self.total)


class Matrix(BaseModel):
    """Expected label to observed label, counted.

    Kept whole rather than reduced to an accuracy, because the direction
    of an error is the finding: an ``unsafe`` command called ``safe`` and
    a ``safe`` one called ``unsafe`` are the same one point of accuracy
    and nothing like the same event.
    """

    cells: dict[str, dict[str, int]] = Field(default_factory=dict)

    def record(self, expected: str, observed: str) -> None:
        self.cells.setdefault(expected, {}).setdefault(observed, 0)
        self.cells[expected][observed] += 1

    @property
    def total(self) -> int:
        return sum(n for row in self.cells.values() for n in row.values())

    @property
    def correct(self) -> int:
        return sum(row.get(label, 0) for label, row in self.cells.items())

    @property
    def accuracy(self) -> float | None:
        return _ratio(self.correct, self.total)

    def count(self, expected: str, observed: str) -> int:
        return self.cells.get(expected, {}).get(observed, 0)

    def rate(self, expected: str, observed: str) -> float | None:
        """How often ``expected`` was answered ``observed``.

        None when nothing in the corpus carried that label — a rate over
        no cases is not zero, it is unmeasured.
        """
        row = self.cells.get(expected)
        if not row:
            return None
        return _ratio(row.get(observed, 0), sum(row.values()))

    def labels(self) -> list[str]:
        """Every label seen on either axis, sorted."""
        seen = set(self.cells)
        for row in self.cells.values():
            seen.update(row)
        return sorted(seen)


def enforce_decision(verdict: str) -> str:
    """What ``enforce`` does with a verdict under ``no_verdict = "ask"``.

    The hook's rule, restated for the evals that price it: ``unsafe`` is
    refused, a verdict that is not one — ``unclear``, or none in time — is
    asked, and ``safe`` passes. Kept beside the metrics rather than imported
    from the hook, whose import graph is a budget.
    """
    if verdict == "unsafe":
        return "refused"
    if verdict in ("unclear", "unreviewed", "unanswered"):
        return "asked"
    return "allowed"


class Outcome(BaseModel):
    """One case, under one measurement. The row a person reads to debug."""

    case: str
    expected: str
    observed: str
    ok: bool
    detail: str = ""
    """What the answer said, briefly — the model's reason, or which rule
    fired. Empty when there is nothing to add to the two labels."""


class Measurement(BaseModel):
    """One question, asked of the whole corpus.

    ``question`` is what the number means, in a sentence. It is stored
    with the result rather than left in a renderer because a rate whose
    question nobody wrote down is a number people argue about later.
    """

    id: str
    question: str
    tier: Tier = "mechanical"
    counts: Counts | None = None
    matrix: Matrix | None = None
    outcomes: list[Outcome] = Field(default_factory=list)
    unanswered: int = 0
    """Cases the model was asked about and did not answer. Never folded
    into a wrong answer: no verdict is not the same as the wrong one, the
    same rule ``unreviewed`` keeps everywhere else in this codebase."""
    note: str = ""
    """Why this was not measured, when it was not. A measurement carrying
    a note and no table did not run."""

    @property
    def agreement(self) -> float | None:
        """Of the cases with a label, how many landed on it.

        The third shape, for a question that is neither a two-way fire /
        do-not-fire nor a pick-one-label: a severity has to fall inside a
        band, and a band is several labels wide because the judgement
        genuinely is. None when nothing carried a label.
        """
        return _ratio(sum(1 for o in self.outcomes if o.ok), len(self.outcomes))

    @property
    def scored(self) -> int:
        """Outcomes that actually carry a verdict.

        The denominator of :attr:`headline`, and it moves: a run where the
        backend answered about two cases out of twelve produces a rate over
        two. Reported beside every rate for that reason — a smaller
        denominator can only make a score look better, and a comparison
        that hides it is one that rewards an outage.
        """
        if self.matrix is not None:
            return self.matrix.total
        if self.counts is not None:
            return self.counts.total
        return len(self.outcomes)

    @property
    def headline(self) -> tuple[str, float | None]:
        """The one number this measurement is read by, and its name.

        Recall for a two-way stage, because a miss is what those exist to
        prevent; accuracy for a labelled one; the on-label rate otherwise.
        It exists for iteration: comparing a prompt before and after means
        comparing one number per measurement across several runs, and
        picking a different one each time is how you talk yourself into an
        improvement.
        """
        if self.counts is not None:
            return "recall", self.counts.recall
        if self.matrix is not None:
            return "accuracy", self.matrix.accuracy
        return "on label", self.agreement

    @property
    def ran(self) -> bool:
        return bool(self.outcomes) or self.counts is not None or self.matrix is not None

    @property
    def failures(self) -> list[Outcome]:
        return [o for o in self.outcomes if not o.ok]


class Suite(BaseModel):
    """Everything one eval command measured, and under what conditions.

    ``used_model`` is the same fact ``DriftReport.analyzed`` records, for
    the same reason: a suite that could not reach a backend has not found
    that the judged tier is fine.
    """

    id: str
    title: str
    measurements: list[Measurement] = Field(default_factory=list)
    used_model: bool = False
    model: str = ""
    endpoint: str = ""
    duration_s: float = 0.0
    note: str = ""

    @property
    def rows(self) -> int:
        """Labelled outcomes across every measurement.

        Not the size of the corpus: one case is measured several ways, and
        one case naming three dangers is three rows in the measurement
        that asks whether each was named.
        """
        return sum(len(m.outcomes) for m in self.measurements)


def spread(
    runs: list[Suite],
) -> dict[str, tuple[str, list[tuple[float | None, int]]]]:
    """Each measurement's headline number, once per run.

    A drift answer is not deterministic — the gates ask at temperature 0.0
    and the drift pass does not — so a single run cannot tell a prompt
    that improved from one that got a luckier sample. This is what a
    before-and-after is read from.
    """
    out: dict[str, tuple[str, list[tuple[float | None, int]]]] = {}
    for suite in runs:
        for measurement in suite.measurements:
            name, value = measurement.headline
            if measurement.id not in out:
                out[measurement.id] = (name, [])
            out[measurement.id][1].append((value, measurement.scored))
    return out


def stability(runs: list[Suite]) -> dict[str, float | None]:
    """Per measurement, how often repeated runs gave the same answer.

    The gates ask at ``temperature = 0.0`` and the docstrings say a gate
    "answers the same way twice". That is a claim about a server, not a
    theorem — a batched backend can reorder a fan-out, and a quantised
    one can round differently — so it is worth a number. None where a
    measurement produced no cases to compare.
    """
    if len(runs) < 2:
        return {}
    out: dict[str, float | None] = {}
    for first in runs[0].measurements:
        others = [
            {o.case: o.observed for o in m.outcomes}
            for suite in runs[1:]
            for m in suite.measurements
            if m.id == first.id
        ]
        if not others or not first.outcomes:
            out[first.id] = None
            continue
        agreed = sum(
            1
            for o in first.outcomes
            if all(other.get(o.case) == o.observed for other in others)
        )
        out[first.id] = agreed / len(first.outcomes)
    return out
