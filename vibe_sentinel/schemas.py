"""The data that moves through the pipeline.

    template ──fill──▶ concrete command ──run──▶ ProbeResult
                                                     │
                                                     ▼
                                              [Observation]
                                                     │
                                          inventory + compare
                                                     │
                                                     ▼
                                                 [Change] ──▶ DriftReport

The command journal's models are not here: they are dataclasses in
:mod:`vibe_sentinel.journal`, because that boundary is a hook payload
rather than a language model. The reasoning is in that module's
docstring.

An :class:`Observation` is one structural fact with a stable ``key``.
The key is what makes drift detectable: the same directory, module, or
pattern measured a week apart lands under the same key, so a value can
be compared and a key that was not there before is, by itself, the
finding. "Code started appearing somewhere new" needs no threshold.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from vibe_sentinel.json_schema import unbounded_schema

Severity = Literal["high", "medium", "low", "info"]
ChangeKind = Literal["appeared", "disappeared", "grew", "shrank"]
SafetyVerdict = Literal["safe", "unclear", "unsafe"]
SecretVerdict = Literal["real", "placeholder", "unclear"]
NearMissVerdict = Literal["distinct", "typosquat", "unclear"]


class Observation(BaseModel):
    """One structural fact about the codebase.

    ``key`` identifies the thing being measured and must be stable across
    runs — a package path, a module name, a pattern id. ``value`` is the
    measured number where one applies; observations that only record
    existence (a pattern was seen at all) leave it None.
    """

    key: str
    value: float | None = None
    label: str = ""
    attrs: dict[str, str] = Field(default_factory=dict)

    risk: str = ""
    """What the probe measured about this fact's provenance, if anything.

    A short mechanical label — ``orphan``, ``phantom``, ``squatted`` — set by
    the probe itself and stored verbatim. It is not a severity and not an
    opinion: the model never writes this field, exactly as it never adds or
    removes a change. Empty for observations that carry no such measurement,
    which is most of them.
    """


class ProbeResult(BaseModel):
    """What one probe produced on one run."""

    probe_id: str
    title: str = ""
    command: list[str] = Field(default_factory=list)
    filled: dict[str, str] = Field(default_factory=dict)
    """Placeholder values the model supplied for this run."""
    observations: list[Observation] = Field(default_factory=list)
    summary: str = ""
    ok: bool = True
    error: str = ""
    duration_ms: int = 0

    def by_key(self) -> dict[str, Observation]:
        return {o.key: o for o in self.observations}


class Snapshot(BaseModel):
    """The recorded structure of the codebase at one point in time.

    This is the inventory: what the organization looked like, so the next
    run has something to compare against.
    """

    version: int = 1
    generated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    root: str = ""
    model: str = ""
    used_model: bool = True
    probes: dict[str, ProbeResult] = Field(default_factory=dict)

    def probe_keys(self, probe_id: str) -> set[str]:
        result = self.probes.get(probe_id)
        return set(result.by_key()) if result else set()


class Change(BaseModel):
    """One difference between the recorded inventory and this run."""

    probe_id: str
    key: str
    kind: ChangeKind
    before: float | None = None
    after: float | None = None
    label: str = ""
    severity: Severity = "info"
    note: str = ""
    """Filled by the analysis pass — why this change matters, or doesn't."""

    @property
    def delta(self) -> float | None:
        if self.before is None or self.after is None:
            return None
        return self.after - self.before

    def describe(self) -> str:
        if self.kind == "appeared":
            return f"new: {self.label or self.key}"
        if self.kind == "disappeared":
            return f"gone: {self.label or self.key}"
        before = "?" if self.before is None else f"{self.before:g}"
        after = "?" if self.after is None else f"{self.after:g}"
        return f"{self.label or self.key}: {before} -> {after}"


class HorizonDrift(BaseModel):
    """What moved over one declared horizon, against a run already recorded.

    A horizon is a *reading*, not a second baseline, and three things
    follow from that. Nothing here moves the baseline marker. Nothing
    here changes an exit code — a month-old finding would otherwise fail
    every scan for a month with nothing that clears it, which is the
    report-forever-with-no-decision shape that made licences-as-probes
    wrong. And nothing here is stored: both ends are already in the
    history, so this diff can be recomputed exactly, which the
    measurements it is derived from cannot be. The database keeps what
    cannot be regenerated.

    ``run_id`` is empty when no run reaches back that far, and
    ``unavailable`` then says so. A horizon that could not be measured
    must not render as a horizon that found nothing.
    """

    horizon: str
    """The horizon as declared — ``1w``, ``3m``."""
    run_id: int | None = None
    at: str = ""
    age_days: float = 0.0
    """How old the selected run actually is. The horizon picks the newest
    run *at least* that old, which can be considerably older, and a
    comparison labelled ``1w`` that reached back eleven days says so."""
    changes: list[Change] = Field(default_factory=list)
    unavailable: str = ""
    """Why nothing reached back this far. Empty when something did."""

    @property
    def moved(self) -> bool:
        return any(c.severity != "info" for c in self.changes)


class LensResult(BaseModel):
    """What one declared lens made of the report, and whether it was asked.

    Three states, kept apart for the reason ``HorizonDrift.unavailable``
    exists: a lens nothing it watches triggered, a lens that was asked and
    did not answer, and a lens that answered are three different things and
    none of them is "found nothing".
    """

    id: str
    title: str = ""
    asked: bool = False
    """Whether a question was actually sent. False when nothing this lens
    watches moved, and false for every lens under ``--no-model``."""
    answered: bool = False
    """Whether the model came back with something usable."""
    assessment: str = ""
    rated: int = 0
    """How many changes this lens rated."""
    note: str = ""
    """Why it was not asked, or why its answer was unusable. Empty when it
    answered."""


class DriftReport(BaseModel):
    """The verdict of one comparison against the recorded inventory."""

    baseline_at: str = ""
    current_at: str = ""
    changes: list[Change] = Field(default_factory=list)
    horizons: list[HorizonDrift] = Field(default_factory=list)
    """The same comparison run again against longer horizons. Reported
    beside ``changes``, never merged into it: these are measured from a
    different point in time, no model rated them, and they do not count
    towards :attr:`drifted`."""
    trends: list[TrendFit] = Field(default_factory=list)
    """What the whole recorded series is doing, fitted. Beside the
    changes for the same reasons as ``horizons`` — a different span and
    outside :attr:`drifted`. A lens may read both, and say so in its
    assessment; neither becomes a change, moves a baseline, or reaches an
    exit code by being read."""
    lenses: list[LensResult] = Field(default_factory=list)
    """One entry per declared lens, asked or not. The accounting for the
    analysis pass: which questions this project asks of a drift report,
    and which of them actually got an answer this run."""
    assessment: str = ""
    """The analysis pass's read of the changes as a whole."""
    first_run: bool = False
    """True when there was no inventory to compare against yet."""
    analyzed: bool = False
    """True when the model rated these changes. False means the
    severities are the mechanical ones from the comparison, and no
    rendering may claim the drift was reviewed."""

    @property
    def drifted(self) -> bool:
        """True when anything worth reporting changed.

        ``info`` changes are recorded but do not count as drift — a
        module gaining four lines is not a structural event, and a tool
        that calls it one gets muted. Neither do the horizon comparisons
        in :attr:`horizons` or the fits in :attr:`trends`: what moved
        since the baseline is what ``--update`` accepts, and neither a
        horizon nor a slope has any such decision to make.
        """
        return any(c.severity != "info" for c in self.changes)


class RunRecord(BaseModel):
    """One row of the runs table — a scan's identity and headline counts."""

    id: int
    started_at: str
    root: str
    model: str = ""
    used_model: bool = True
    analyzed: bool = False
    is_baseline: bool = False
    probe_count: int = 0
    observation_count: int = 0
    change_count: int = 0


class TrendPoint(BaseModel):
    """One observation's value at one point in the run history."""

    run_id: int
    at: str
    value: float | None = None
    label: str = ""


class Anomaly(BaseModel):
    """One value that departed from its own fitted trend.

    Not "far from average" — far from where the series was going. In a
    directory that has been growing for months every early value is far
    from the mean and none of them is a surprise.

    ``run_id`` is None for a value the current scan just measured, which
    has no run of its own yet: the fit is made from the recorded history
    and the new value scored against it, out of sample.
    """

    run_id: int | None = None
    at: str = ""
    value: float = 0.0
    expected: float = 0.0
    """Where the fit put this point."""
    z: float | None = None
    """Departure in robust deviations. Signed: the direction is the
    finding as much as the size is.

    None when the series held one value on every recorded run: there is
    no spread and no step to measure a departure in. There is no score to compute then, and
    that is not the same as nothing having happened: a series that held
    at 14 for thirty runs and is now 40 has departed from the only thing
    it ever did. Rendering says that in words rather than inventing a
    number for it."""


class TrendFit(BaseModel):
    """One observation's history, fitted.

    A reading over the whole recorded series rather than a comparison of
    two points, which is the only way a direction can be seen at all.
    Like a horizon it never moves the baseline, is never stored, and
    never reaches an exit code — a trend persists for as many scans as
    the window is long, and nothing anyone could do would clear it.
    """

    probe_id: str
    key: str
    label: str = ""

    runs: int = 0
    first_run: int = 0
    last_run: int = 0
    first_value: float = 0.0
    last_value: float = 0.0

    slope: float = 0.0
    """Theil–Sen slope, in units of the observation per run."""
    intercept: float = 0.0
    fitted_change: float = 0.0
    """What the slope accounts for across the fitted span — the honest
    headline, because the endpoints include whatever noise they carry."""

    tau: float = 0.0
    """Kendall's tau-b: how consistently one-way the movement is, from
    -1 to +1. The effect size beside the p-value, because a direction can
    be certain and negligible at once."""
    p_value: float = 1.0
    significant: bool = False
    direction: Literal["rising", "falling", "flat"] = "flat"

    scale: float = 0.0
    """Robust spread of the residuals — what an anomaly is measured in."""
    anomalies: list[Anomaly] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# LLM-facing models
# ---------------------------------------------------------------------------
#
# Every string a model writes is bounded here, and none of those bounds
# goes on the wire — a single ``maxLength`` collapses some grammar
# compilers (see :mod:`vibe_sentinel.json_schema`). They are enforced in
# three places instead: the prompt quotes the number, ``clip_to_bounds``
# trims an over-run before validation, and the field below is the check
# that catches a bug in the trimming.
#
# The numbers are ~2x a natural answer, and they are measured. Before they
# existed, four lenses answered about a 21-change report on this repository
# in 1322, 1432, 1762 and 2225 tokens — roughly 190 tokens for every change
# rated, which is five sentences where the prompt asked for one. The widest
# of the four overran the 2048-token default and came back as half an
# answer. An unbounded answer is not only expensive; it is the one that
# does not arrive.

#: One sentence about one change.
NOTE_CHARS = 240
#: Two or three sentences about the report as a whole.
ASSESSMENT_CHARS = 700
#: Ratings one lens may return. A report with more changes than this is
#: not one where rating every change helps, and the ones past the cap keep
#: the mechanical severity — the same degradation as a lens that did not
#: mention them.
MAX_RATED = 40
#: A reason a person acts on: the command that made this one dangerous, or
#: why a string is a live key.
REASON_CHARS = 400

#: Characters per English word, with its space and punctuation. Used only
#: to turn a character cap into something a model can actually count.
_CHARS_PER_WORD = 8


def chars_to_words(chars: int) -> int:
    """A character cap, said as a word count a model can hold to.

    Every bound here is in characters, because that is what the clipping
    enforces. No model counts characters. It does approximately count
    words, so the prompt quotes words and the schema keeps characters,
    and the conversion happens in one place rather than in each prompt.

    Rounded up to the nearest five so the sentence reads as guidance
    rather than as a checksum — "under 50 words", not "under 47".
    """
    return ((max(chars, 0) // _CHARS_PER_WORD) + 4) // 5 * 5


#: Said to the model in every prompt that has a bounded reason, so the cap
#: it is held to and the cap it is told about are one number. Appended by
#: each question builder rather than baked into a system prompt, because
#: the gates that ask these questions must not import this module until
#: they have decided to ask — the hook's import budget is the reason.
BREVITY = (
    f'Keep "reason" to one sentence, under {chars_to_words(REASON_CHARS)} words. '
    f"Anything past that is cut off, so say the deciding thing first."
)


class ChangeAssessment(BaseModel):
    """The model's read of one structural change.

    ``probe_id`` and ``key`` together name the change being rated, which is
    the same identity the ``changes`` table stores and indexes. The key alone
    is not that identity: two probes measuring the same directory produce the
    same key, and a rating attached by key alone lands on whichever of them
    was seen last. It defaults to empty because a model that answers with only
    a key is still answering usefully wherever that key names one change.
    """

    key: str = Field(default="", max_length=200)
    probe_id: str = Field(default="", max_length=64)
    severity: Severity = "info"
    note: str = Field(default="", max_length=NOTE_CHARS)


class DriftAnalysis(BaseModel):
    """What the model returns when asked to analyze a set of changes."""

    assessment: str = Field(default="", max_length=ASSESSMENT_CHARS)
    changes: list[ChangeAssessment] = Field(default_factory=list, max_length=MAX_RATED)


class LicenceDraft(BaseModel):
    """The model's read of one package's licence evidence.

    Advisory only, and structurally unable to be anything else: the gate's
    verdict comes from :func:`vibe_sentinel.licenses.resolve`, which never sees
    this. What it produces is the thing a human would otherwise write by hand
    before pinning — a candidate identifier and a first draft of the reason —
    for that human to check and sign.
    """

    identifier: str = Field(default="", max_length=100)
    confidence: Literal["high", "medium", "low"] = "low"
    reason: str = Field(default="", max_length=ASSESSMENT_CHARS)
    verify: str = Field(default="", max_length=REASON_CHARS)


class SafetyOpinion(BaseModel):
    """The model's read of one command it was asked to judge.

    ``verdict`` is the only field with consequences, and the three values
    are deliberately not a score: a gate that fires on a threshold
    invites tuning the threshold until it stops firing. ``unclear`` is a
    real answer — it means the model could not tell from the history it
    was given, which is different from "fine" and different from
    "dangerous", and in enforce mode it asks rather than blocks.

    ``reason`` is shown to whoever has to act on it. It should name the
    earlier command that made this one dangerous, when there is one,
    because that is the part a person cannot reconstruct quickly.
    """

    verdict: SafetyVerdict = "unclear"
    reason: str = Field(default="", max_length=REASON_CHARS)
    resolves_to: str = Field(default="", max_length=200)
    """What the model believes the command will actually act on, once
    variables and globs are resolved from the history. Empty when it
    cannot tell — which is itself worth recording."""


class SecretOpinion(BaseModel):
    """The model's read of one candidate credential.

    Three values, and ``unclear`` is again a real answer rather than a
    hedge: "I cannot tell whether this forty-character string is live"
    is exactly the finding a person needs to see, and it is different
    from both "fine" and "this is a key".

    The model never sees a credential in full — values reach it as a
    short prefix and a shape (length, entropy). ``reason`` should say
    what in that shape decided it, because that is the part a person
    cannot reconstruct from the redacted excerpt they are shown.
    """

    verdict: SecretVerdict = "unclear"
    reason: str = Field(default="", max_length=REASON_CHARS)


class NearMissOpinion(BaseModel):
    """The model's read of two installed names one edit apart.

    The one provenance check with a judgement in it. Every other kind the
    audit reports is a fact — an import resolves or it does not, a
    version is bounded or it is not — but "``httpx`` and ``httpx2`` are
    both installed" is a typosquat and a rewrite shipped under a new name
    in exactly the same shape, and an edit distance cannot tell them
    apart. The rule already carries one exception found by hand, on this
    repository's own environment, which is what a judgement dressed as a
    rule looks like.

    ``suspect`` is which of the two names is the imposter, when there is
    one. The audit guesses — it picks whichever name nothing asked for —
    and that guess never becomes the finding's key, because a key that
    moved with an opinion would not be stable across runs.
    """

    verdict: NearMissVerdict = "unclear"
    suspect: str = Field(default="", max_length=100)
    reason: str = Field(default="", max_length=REASON_CHARS)


class GateFinding(BaseModel):
    """One thing a gate found to be true of the tree, right now.

    Not a :class:`Change`. A change is a transition and reports once; this
    is a state and reports on every run until somebody removes the cause
    or writes a pin. A key committed six months ago is exactly as
    committed today, and a diff cannot say so — it only ever spoke about
    the moment the key arrived.

    ``key`` is stable across runs for the same fact — ``<rule>:<path>``
    for a credential, ``<kind>:<name>`` for a provenance finding, the
    package name for a licence. It is what lets the history answer "when
    did this start" without the finding itself being a diff.

    ``adjudicated`` carries the same honesty as ``DriftReport.analyzed``
    and ``command_reviews.reviewed``: false means the verdict is
    mechanical and no rendering may call it a review.
    """

    gate: str
    key: str
    kind: str = ""
    subject: str = ""
    label: str = ""
    detail: str = ""
    risk: str = ""
    """Where this sits relative to git, for gates that can tell. A
    measured fact about the finding, never an opinion about it."""
    verdict: str = ""
    failing: bool = False
    """Whether this one fails the gate. A finding that a pin already
    settles is recorded and does not fail."""
    pinned: bool = False
    adjudicated: bool = False
    reason: str = ""
    attrs: dict[str, str] = Field(default_factory=dict)


class GateReport(BaseModel):
    """One gate's answer about the tree, and whether it got to finish.

    ``ok`` false means the gate could not complete — a policy that will
    not load, a walk that hit ``max_files``. Recorded rather than raised,
    for the reason a failed probe is: one broken gate must not lose the
    others, and a gate that silently reported nothing would read on the
    next run as a tree that had been cleaned up.
    """

    gate: str
    ok: bool = True
    configured: bool = True
    """False when this project never declared a policy for this gate.
    Distinct from ``ok`` on purpose: a gate nobody configured has not
    failed, and must not fail a scan — but it has not passed either, and
    a report that showed it as clean would be inventing a result."""
    adjudicated: bool = False
    findings: tuple[GateFinding, ...] = ()
    summary: str = ""
    error: str = ""
    duration_ms: int = 0

    @property
    def failing(self) -> tuple[GateFinding, ...]:
        return tuple(f for f in self.findings if f.failing)


class GateState(BaseModel):
    """What every gate says about the tree on one run.

    Deliberately not part of :class:`DriftReport`. Drift is what moved;
    this is what is true, and folding the second into the first is what
    let a committed key sit in a baseline and never be mentioned again.
    """

    reports: tuple[GateReport, ...] = ()

    @property
    def failing(self) -> tuple[GateFinding, ...]:
        return tuple(f for r in self.reports for f in r.failing)

    @property
    def broken(self) -> tuple[GateReport, ...]:
        """Gates that could not complete. Never silently clean."""
        return tuple(r for r in self.reports if not r.ok and r.configured)

    @property
    def unconfigured(self) -> tuple[GateReport, ...]:
        """Gates this project never set a policy for."""
        return tuple(r for r in self.reports if not r.configured)


_SAFETY_SCHEMA = unbounded_schema(SafetyOpinion)
_SECRET_SCHEMA = unbounded_schema(SecretOpinion)
_NEAR_MISS_SCHEMA = unbounded_schema(NearMissOpinion)
_ANALYSIS_SCHEMA = unbounded_schema(DriftAnalysis)
_LICENCE_DRAFT_SCHEMA = unbounded_schema(LicenceDraft)
