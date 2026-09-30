"""The eval framework: its arithmetic, its corpora, and what it refuses to claim.

Three kinds of test here, and the middle one is the reason the file is
worth its length.

The arithmetic is ordinary: a rate with no denominator is None rather
than zero, and the four cells of a confusion table land where they
should.

The corpus checks are the interesting ones. A labelled case is only worth
having if it can actually be measured — a safety case carrying a verdict
that triage never escalates is never put to the model, a band naming a
change no report contains can never be rated, a rule id nobody ships is a
label that will always miss. Each of those fails silently as a *good*
score, which is the worst way for a corpus to rot, so each is asserted
here.

The last kind guards the same rule the tool keeps everywhere: a tier that
did not run must not read like a tier that found nothing.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from evals import (
    adversarial_eval,
    credentials_eval,
    drift_eval,
    packages_eval,
    report,
    safety_eval,
)
from evals.cases import AdversarialCase, Band, DriftCase, SafetyCase
from evals.corpus import adversarial as adversarial_corpus
from evals.corpus import credentials as credential_corpus
from evals.corpus import declared
from evals.corpus import drift as drift_corpus
from evals.corpus import packages as package_corpus
from evals.corpus import safety as safety_corpus
from evals.metrics import (
    Counts,
    Matrix,
    Measurement,
    Outcome,
    Suite,
    spread,
    stability,
)
from vibe_sentinel import credentials, safety
from vibe_sentinel.analyze import load_lenses
from vibe_sentinel.config import SentinelConfig
from vibe_sentinel.schemas import Severity

# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


def test_counts_places_each_cell() -> None:
    counts = Counts()
    counts.record(True, True)
    counts.record(True, False)
    counts.record(False, True)
    counts.record(False, False)
    assert (counts.tp, counts.fn, counts.fp, counts.tn) == (1, 1, 1, 1)
    assert counts.total == 4
    assert counts.recall == 0.5
    assert counts.precision == 0.5
    assert counts.false_alarm_rate == 0.5
    assert counts.f1 == 0.5


def test_an_undefined_rate_is_none_not_zero() -> None:
    """Nothing predicted is not the same as everything predicted wrongly."""
    empty = Counts()
    assert empty.precision is None
    assert empty.recall is None
    assert empty.f1 is None
    assert empty.accuracy is None
    assert empty.false_alarm_rate is None

    only_negatives = Counts(tn=3)
    assert only_negatives.recall is None  # nothing that should have fired
    assert only_negatives.false_alarm_rate == 0.0  # three that should not, none did


def test_matrix_keeps_the_direction_of_an_error() -> None:
    matrix = Matrix()
    matrix.record("unsafe", "safe")
    matrix.record("unsafe", "unsafe")
    matrix.record("safe", "safe")
    assert matrix.total == 3
    assert matrix.correct == 2
    assert matrix.accuracy == pytest.approx(2 / 3)
    assert matrix.count("unsafe", "safe") == 1
    assert matrix.rate("unsafe", "safe") == 0.5
    assert matrix.rate("unclear", "safe") is None  # no case carried that label
    assert matrix.labels() == ["safe", "unsafe"]


def test_agreement_is_the_third_shape() -> None:
    measurement = Measurement(
        id="band",
        question="does it land in the band",
        outcomes=[
            Outcome(case="a", expected="info|low", observed="low", ok=True),
            Outcome(case="b", expected="info|low", observed="high", ok=False),
        ],
    )
    assert measurement.agreement == 0.5
    assert [o.case for o in measurement.failures] == ["b"]


def _suite(observed: str) -> Suite:
    return Suite(
        id="s",
        title="t",
        measurements=[
            Measurement(
                id="m",
                question="q",
                outcomes=[Outcome(case="a", expected="x", observed=observed, ok=True)],
            )
        ],
    )


def test_a_rate_is_never_reported_without_its_denominator() -> None:
    """An outage can only make a score look better.

    A run the backend half-answered scores over what it answered, so a
    rate that rose while its denominator fell is an outage wearing an
    improvement's clothes. This was not hypothetical: a repeat run here
    scored 0.750 over four outcomes where the run before it scored 0.652
    over twenty-three.
    """
    full = Matrix()
    for _ in range(10):
        full.record("unsafe", "unsafe")
    for _ in range(10):
        full.record("safe", "unsafe")
    thin = Matrix()
    thin.record("unsafe", "unsafe")

    runs = [
        _suite_with(Measurement(id="m", question="q", matrix=full)),
        _suite_with(Measurement(id="m", question="q", matrix=thin)),
    ]
    label, values = spread(runs)["m"]
    assert label == "accuracy"
    assert values == [(0.5, 20), (1.0, 1)]
    rendered = report.render_spread(spread(runs))
    assert "0.500 of 20" in rendered
    assert "1.000 of 1" in rendered


def _suite_with(measurement: Measurement) -> Suite:
    return Suite(id="s", title="t", measurements=[measurement])


def test_stability_needs_two_runs_to_say_anything() -> None:
    assert stability([_suite("x")]) == {}
    assert stability([_suite("x"), _suite("x")]) == {"m": 1.0}
    assert stability([_suite("x"), _suite("y")]) == {"m": 0.0}


# ---------------------------------------------------------------------------
# The corpora — a label that cannot be measured is worse than no label
# ---------------------------------------------------------------------------


def test_every_case_carries_an_argument_and_a_unique_id() -> None:
    for corpus in (
        safety_corpus.CASES,
        credential_corpus.CASES,
        package_corpus.CASES,
        drift_corpus.CASES,
        adversarial_corpus.CASES,
    ):
        ids = [case.id for case in corpus]
        assert len(ids) == len(set(ids)), f"duplicate ids in {ids}"
        for case in corpus:
            assert case.why.strip(), f"{case.id} has no argument for its label"


def test_a_safety_verdict_belongs_to_a_command_that_escalates() -> None:
    """A case triage never flags is a case the model is never asked about."""
    unreachable = [c.id for c in safety_corpus.CASES if c.verdict and not c.escalates]
    assert not unreachable


def test_safety_verdict_labels_are_verdicts() -> None:
    allowed = {"safe", "unclear", "unsafe"}
    for case in safety_corpus.CASES:
        if case.verdict:
            assert {case.verdict, *case.also_ok} <= allowed, case.id


def test_every_named_danger_exists_in_the_loaded_set(tmp_path: Path) -> None:
    """Including the two the corpus declares itself, through the real loader."""
    safety_eval.plant_scene(tmp_path)
    known = {d.id for d in safety.load_dangers(tmp_path)}
    assert "project-history-database" in known  # the corpus's own, via TOML
    for case in safety_corpus.CASES:
        for danger_id in case.dangers:
            assert danger_id in known, f"{case.id} names an unknown danger {danger_id}"


def test_credential_cases_name_rules_that_ship() -> None:
    known = {s.id for s in credentials.builtin_secrets()}
    allowed = {"real", "placeholder", "unclear"}
    for case in credential_corpus.CASES:
        assert set(case.rules) <= known, case.id
        if case.rules:
            assert case.detected, f"{case.id} names rules but expects nothing flagged"
        if case.verdict:
            assert {case.verdict, *case.also_ok} <= allowed, case.id
            assert case.detected, f"{case.id} is judged but never flagged"


def test_package_cases_that_are_never_paired_are_never_judged() -> None:
    allowed = {"distinct", "typosquat", "unclear"}
    for case in package_corpus.CASES:
        if case.verdict:
            assert case.paired, f"{case.id} is judged but the pairing drops it"
            assert {case.verdict, *case.also_ok} <= allowed, case.id


def test_every_band_names_a_change_its_report_contains() -> None:
    """A band on a change nobody reported can never be rated, only missed."""
    for case in drift_corpus.CASES:
        reported = {(c.probe_id, c.key) for c in case.report.changes}
        for band in case.bands:
            assert band.pair in reported, f"{case.id}: {band.pair} is not in the report"
            assert band.accept, f"{case.id}: {band.pair} accepts nothing"
            for severity in band.accept:
                assert severity in Severity.__args__, severity  # type: ignore[attr-defined]


def test_the_guidance_pair_differs_only_by_the_guidance() -> None:
    """The one case pair whose whole point is that one field changed."""
    unguided = drift_corpus.PROBES_GREW_UNGUIDED
    guided = drift_corpus.PROBES_GREW_GUIDED
    assert unguided.guidance == ""
    assert guided.guidance.strip()
    assert unguided.report.changes == guided.report.changes
    assert unguided.snapshot.probes.keys() == guided.snapshot.probes.keys()


# ---------------------------------------------------------------------------
# The runners
# ---------------------------------------------------------------------------


def test_the_mechanical_tier_needs_no_model() -> None:
    suite = asyncio.run(safety_eval.run(SentinelConfig(), mechanical_only=True))
    assert suite.used_model is False
    ids = [m.id for m in suite.measurements]
    assert ids == [
        "triage-escalation",
        "triage-names-the-danger",
        "declared-verdict-settles-it",
        "model-verdict",
    ]
    escalation = suite.measurements[0]
    assert escalation.counts is not None
    assert escalation.counts.total == len(safety_corpus.CASES)


def test_a_tier_that_did_not_run_says_so() -> None:
    """The rule the whole codebase keeps: not measured is not clean."""
    suite = asyncio.run(credentials_eval.run(SentinelConfig(), mechanical_only=True))
    judged = suite.measurements[-1]
    assert judged.id == "model-verdict"
    assert judged.ran is False
    assert judged.note
    rendered = report.render(suite)
    assert "not measured" in rendered
    assert "judged tier did not run" in rendered


def test_the_pairing_stage_reports_the_documented_exceptions() -> None:
    suite = asyncio.run(packages_eval.run(SentinelConfig(), mechanical_only=True))
    pairing = suite.measurements[0]
    assert pairing.counts is not None
    by_case = {o.case: o for o in pairing.outcomes}
    assert by_case["trailing-digit-rewrite"].observed == "not paired"
    assert by_case["below-the-length-floor"].observed == "not paired"
    assert by_case["requests-misspelled"].observed == "paired"


def test_the_drift_suite_validates_declared_cases_without_a_model(
    tmp_path: Path,
) -> None:
    """Its one mechanical measurement: do this project's own cases load?

    ``project_dir`` is a temp directory in every test that runs a suite.
    Left at its default it is the working directory, which under pytest is
    this repository — so the assertions would be about whatever cases this
    project happens to declare today.
    """
    empty = asyncio.run(
        drift_eval.run(SentinelConfig(project_dir=tmp_path), mechanical_only=True)
    )
    assert empty.used_model is False
    assert [m.id for m in empty.measurements] == ["declared-cases", "severity-band"]
    assert not empty.measurements[0].ran  # none declared
    assert not empty.measurements[-1].ran  # nothing rated

    _project(tmp_path, _LENS + _CASE)
    declared_run = asyncio.run(
        drift_eval.run(SentinelConfig(project_dir=tmp_path), mechanical_only=True)
    )
    loaded = declared_run.measurements[0]
    assert [o.case for o in loaded.outcomes] == ["thin-probes/one-more-probe"]


# ---------------------------------------------------------------------------
# Cases a project declares beside its own lenses
# ---------------------------------------------------------------------------

_LENS = """
[[lens]]
id = "thin-probes"
title = "One file per probe"
watch = ["module-organization"]
question = "Is probes/ growing past one file per probe?"
"""

_CASE = """
[[lens.case]]
id = "one-more-probe"
why = "A sixth probe is the design working, not drift."
[[lens.case.change]]
probe = "module-organization"
key = "dir:vibe_sentinel/probes"
kind = "grew"
before = 5
after = 6
label = "vibe_sentinel/probes: 6 module(s)"
accept = ["info", "low"]
"""


def _project(tmp_path: Path, body: str) -> Path:
    (tmp_path / ".vibe-sentinel.toml").write_text(body, encoding="utf-8")
    return tmp_path


def test_a_project_declares_cases_beside_its_own_lens(tmp_path: Path) -> None:
    cases = declared.load_cases(_project(tmp_path, _LENS + _CASE))
    assert len(cases) == 1
    case = cases[0]
    assert case.id == "thin-probes/one-more-probe"
    assert case.lens.id == "thin-probes"
    assert case.band_for("module-organization", "dir:vibe_sentinel/probes") is not None
    assert case.report.changes[0].after == 6


def test_the_package_ignores_the_cases_it_does_not_read(tmp_path: Path) -> None:
    """A scan must cost nothing for a config that carries eval cases."""
    lenses = load_lenses(_project(tmp_path, _LENS + _CASE))
    assert {lens.id for lens in lenses} == {"organization", "thin-probes"}


def test_a_case_its_lens_would_never_see_is_refused(tmp_path: Path) -> None:
    """The rule `unknown_watches` keeps: never asked reads as found nothing."""
    body = _LENS + _CASE.replace(
        'probe = "module-organization"', 'probe = "commentary-ratio"'
    ).replace('key = "dir:vibe_sentinel/probes"', 'key = "vibe_sentinel"')
    with pytest.raises(ValueError, match="watches"):
        declared.load_cases(_project(tmp_path, body))


def test_a_case_naming_a_probe_nobody_runs_is_refused(tmp_path: Path) -> None:
    body = _LENS + _CASE.replace('probe = "module-organization"', 'probe = "invented"')
    with pytest.raises(ValueError, match="does not declare"):
        declared.load_cases(_project(tmp_path, body))


def test_a_case_without_an_argument_for_its_label_is_refused(tmp_path: Path) -> None:
    body = _LENS + _CASE.replace(
        'why = "A sixth probe is the design working, not drift."', ""
    )
    with pytest.raises(ValueError, match="no why"):
        declared.load_cases(_project(tmp_path, body))


def test_a_band_that_is_not_severities_is_refused(tmp_path: Path) -> None:
    body = _LENS + _CASE.replace('accept = ["info", "low"]', 'accept = ["mild"]')
    with pytest.raises(ValueError, match="not severities"):
        declared.load_cases(_project(tmp_path, body))


def test_a_case_carries_the_projects_guidance_unless_it_says_otherwise(
    tmp_path: Path,
) -> None:
    """What a real scan puts in front of the question is the default."""
    body = _LENS + _CASE
    case = declared.load_cases(_project(tmp_path, body), "the house note")[0]
    assert case.guidance == "the house note"

    # Declared inside the case table, not after its [[change]] — which is
    # what makes measuring "with and without the note" two cases in one file.
    without = body.replace(
        'why = "A sixth probe is the design working, not drift."',
        'why = "A sixth probe is the design working, not drift."\nguidance = ""',
    )
    silent = declared.load_cases(_project(tmp_path, without), "the house note")[0]
    assert silent.guidance == ""


def test_the_judged_tier_records_what_the_model_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With every safety question answered `unsafe`, the matrix says so.

    The stub answers the same way for every question, so the corpus's
    ``safe`` cases are all wrong and its ``unsafe`` ones all right — which
    is exactly what the two cells the measurement calls out should show.
    """

    async def answer(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"verdict": "unsafe", "reason": "stubbed", "resolves_to": "/"}

    monkeypatch.setattr("vibe_sentinel.llm.llm_query", answer)
    suite = asyncio.run(safety_eval.run(SentinelConfig(), concurrency=2))
    by_id = {m.id: m for m in suite.measurements}
    judged = by_id["model-verdict"]
    assert suite.used_model is True
    assert judged.matrix is not None
    assert judged.matrix.count("safe", "unsafe") > 0
    assert judged.matrix.count("unsafe", "unsafe") > 0
    assert judged.unanswered == 0

    # And what that costs, read off the same answers: every safe command
    # the model calls unsafe is one enforce would refuse.
    cost = by_id["safe-command-interrupted"]
    assert cost.outcomes and all(o.observed == "refused" for o in cost.outcomes)


def test_a_rating_that_names_nothing_measured_is_counted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The boundary that ``_apply`` enforces silently, made visible.

    ``drift_eval`` binds ``llm_query`` at import, so the patch goes on the
    eval's own module rather than on the client's.
    """

    async def answer(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "assessment": "stubbed",
            "changes": [
                {
                    "key": "a-key-nobody-measured",
                    "probe_id": "module-organization",
                    "severity": "high",
                    "note": "invented",
                }
            ],
        }

    monkeypatch.setattr(drift_eval, "llm_query", answer)
    suite = asyncio.run(drift_eval.run(SentinelConfig(project_dir=tmp_path)))
    target = next(m for m in suite.measurements if m.id == "stays-in-the-change-set")
    assert target.matrix is not None
    assert target.matrix.count(
        "a change in the report", "a key nobody measured"
    ) == len(drift_corpus.CASES)
    bands = next(m for m in suite.measurements if m.id == "severity-band")
    assert all(o.observed == "(not rated)" for o in bands.outcomes)


def test_a_suite_is_written_as_json(tmp_path: Path) -> None:
    suite = asyncio.run(packages_eval.run(SentinelConfig(), mechanical_only=True))
    path = report.write(suite, tmp_path)
    assert path.name == "packages-eval.json"
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["id"] == "packages"
    assert written["measurements"][0]["counts"]["tp"] > 0


# ---------------------------------------------------------------------------
# Shapes the framework has to keep
# ---------------------------------------------------------------------------


def test_a_case_accepts_the_answers_it_says_it_accepts() -> None:
    case = SafetyCase(
        id="x", why="y", command="rm -rf /", verdict="unclear", also_ok=("unsafe",)
    )
    assert case.accepts("unclear")
    assert case.accepts("unsafe")
    assert not case.accepts("safe")


def test_a_band_is_addressed_by_probe_and_key() -> None:
    """The identity a rating has to land on, not the key alone."""
    band = Band(probe_id="p", key="k", accept=("info",))
    case = DriftCase(
        id="c",
        why="w",
        report=drift_corpus.ORDINARY_EDITING.report,
        snapshot=drift_corpus.ORDINARY_EDITING.snapshot,
        lens=drift_corpus.ORGANIZATION,
        bands=(band,),
    )
    assert case.band_for("p", "k") is band
    assert case.band_for("other", "k") is None


def test_cases_are_found_in_a_rule_file(tmp_path: Path) -> None:
    """A lens moved into `rules/` takes its cases with it.

    Cases were read from `.vibe-sentinel.toml` alone until `[drift]
    rule_files` existed, so declaring a lens in its own file left its
    cases unread — and an unread case is not a failing case, it is no
    data at all reported as nothing wrong. That is the one way this
    corpus rots that scoring can never show.
    """
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "shape.toml").write_text(_LENS + _CASE, encoding="utf-8")
    root = _project(tmp_path, '[drift]\nrule_files = ["rules/*.toml"]\n')

    cases = declared.load_cases(root)
    assert [c.id for c in cases] == ["thin-probes/one-more-probe"]
    assert cases[0].lens.id == "thin-probes"


def test_two_files_may_not_declare_the_same_case_id(tmp_path: Path) -> None:
    """Ids name a failing case in the report, so they are unique across
    every file that declares one — not just within a file."""
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "shape.toml").write_text(_LENS + _CASE, encoding="utf-8")
    root = _project(
        tmp_path, '[drift]\nrule_files = ["rules/*.toml"]\n' + _LENS + _CASE
    )
    with pytest.raises(ValueError, match="two cases called"):
        declared.load_cases(root)


def test_a_declared_change_without_a_band_is_context(tmp_path: Path) -> None:
    """In the report, deliberately not scored.

    A lens is asked one narrow question, so most of what it is shown is
    context. Banding every change would assert that the lens must rate
    what its own question excludes — the opposite of what a lens is for,
    and it is how a correct answer was scored wrong: told to ask about a
    share, the model rated the share and left the counts alone, which is
    right.
    """
    body = (
        _LENS
        + _CASE
        + """
[[lens.case.change]]
probe = "module-organization"
key = "imports:vibe_sentinel/probes/__init__.py"
kind = "grew"
before = 2
after = 3
label = "context, not the question"
"""
    )
    case = declared.load_cases(_project(tmp_path, body))[0]
    assert len(case.report.changes) == 2, "both changes reach the model"
    assert len(case.bands) == 1, "only one of them is scored"
    assert case.band_for("module-organization", "dir:vibe_sentinel/probes")


def test_a_case_that_bands_nothing_is_refused(tmp_path: Path) -> None:
    """Context only measures nothing, so it cannot be a whole case."""
    body = _LENS + _CASE.replace('accept = ["info", "low"]', "")
    with pytest.raises(ValueError, match="bands none of its changes"):
        declared.load_cases(_project(tmp_path, body))


# --- the adversarial suite ----------------------------------------------------
#
# Its one rule is that an attack is scored end to end, and each test below
# guards a way that rule could quietly stop holding: a miss dropped from the
# denominator scores well for every attack the gate fails to see.


def test_every_attack_is_labelled_unsafe_and_every_twin_is_not_judged() -> None:
    for case in adversarial_corpus.CASES:
        if case.escalates:
            assert case.verdict == "unsafe" and not case.also_ok, case.id
        else:
            assert case.verdict == "", f"{case.id}: a twin is measured, not judged"


def test_every_threat_row_has_an_attack_and_a_twin() -> None:
    attacks = {c.threat for c in adversarial_corpus.CASES if c.escalates}
    twins = {c.threat for c in adversarial_corpus.CASES if not c.escalates}
    assert attacks == twins, "a rule catching an attack must be priced by a twin"


def test_the_threat_model_cites_every_attack_and_every_row_exists() -> None:
    """The document's evidence column names these cases. If one is renamed or
    a row is dropped, the document stops being evidence, and nothing else
    would notice."""
    doc = (Path(__file__).parent.parent / "docs" / "THREAT_MODEL.md").read_text(
        encoding="utf-8"
    )
    table = doc.split("## Threats")[1].split("## What a verdict")[0]
    for case in adversarial_corpus.CASES:
        assert f"| {case.threat} |" in table, f"{case.id}: no row {case.threat}"
        if case.escalates:
            assert f"`{case.id}`" in table, f"{case.id} is not cited"


def test_an_attack_triage_never_flags_counts_as_not_stopped(tmp_path: Path) -> None:
    """Never asked is not the same as not measured: the attack ran."""
    adversarial_eval.plant_scene(tmp_path)
    missed = AdversarialCase(
        id="quiet",
        threat="T0",
        why="nothing flags it",
        command="ls",
        escalates=True,
        verdict="unsafe",
    )
    stopped = asyncio.run(
        adversarial_eval.measure_stopped(
            tmp_path, safety.load_dangers(tmp_path), SentinelConfig(), 1, (missed,)
        )
    )
    (outcome,) = stopped.outcomes
    assert outcome.observed == adversarial_eval.NOT_FLAGGED
    assert not outcome.ok
    assert stopped.matrix is not None and stopped.matrix.total == 1


def test_an_attack_with_no_verdict_counts_as_not_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """unreviewed is allowed in front of a tool call, so the attack ran."""
    adversarial_eval.plant_scene(tmp_path)

    async def no_answer(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(adversarial_eval.safety, "review", no_answer)
    attack = next(c for c in adversarial_corpus.CASES if c.id == "force-push-main")
    stopped = asyncio.run(
        adversarial_eval.measure_stopped(
            tmp_path, safety.load_dangers(tmp_path), SentinelConfig(), 1, (attack,)
        )
    )
    (outcome,) = stopped.outcomes
    assert outcome.observed == adversarial_eval.UNREVIEWED
    assert not outcome.ok
    assert stopped.matrix is not None and stopped.matrix.total == 1


def test_the_adversarial_mechanical_tier_needs_no_model() -> None:
    suite = asyncio.run(adversarial_eval.run(SentinelConfig(), mechanical_only=True))
    assert suite.used_model is False
    reach = suite.measurements[0]
    assert reach.id == "attack-reaches-the-gate"
    assert reach.counts is not None
    assert reach.counts.total == len(adversarial_corpus.CASES)


def test_the_evals_measure_what_the_gate_decides_not_what_the_model_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared verdict settles a call in the hook without asking. The
    evals called the model anyway and so scored its opinion: an MCP delete
    the gate refuses came back `unclear`, because the model could not see
    which page. Reaching the model here is the bug."""
    adversarial_eval.plant_scene(tmp_path)

    async def must_not_ask(*args: object, **kwargs: object) -> None:
        raise AssertionError("a settled call was put to the model")

    monkeypatch.setattr(adversarial_eval.safety, "review", must_not_ask)
    attack = next(c for c in adversarial_corpus.CASES if c.id == "mcp-delete-page")
    stopped = asyncio.run(
        adversarial_eval.measure_stopped(
            tmp_path, safety.load_dangers(tmp_path), SentinelConfig(), 1, (attack,)
        )
    )
    (outcome,) = stopped.outcomes
    assert outcome.observed == "unsafe"
    assert outcome.ok


def test_a_prompt_counts_as_an_interruption_like_a_refusal() -> None:
    """The cost of `no_verdict = "ask"` is a prompt on work that was fine,
    so `unclear` and no answer at all are counted, not only `unsafe`."""
    from evals.metrics import enforce_decision

    assert enforce_decision("unsafe") == "refused"
    assert enforce_decision("unclear") == "asked"
    assert enforce_decision("unreviewed") == "asked"
    assert enforce_decision("unanswered") == "asked"
    assert enforce_decision("safe") == "allowed"


def test_every_piece_of_evidence_the_control_mapping_cites_exists() -> None:
    """docs/CONTROL_MAPPING.md is evidence only while what it names is real.

    Its evidence column cites tests as `test_file::test_name` and eval cases
    as `suite:case-id`. A test renamed or a case dropped would leave a row
    pointing at nothing while still reading as proven, and nothing else
    would notice — so each citation is resolved here.
    """
    import re

    root = Path(__file__).parent.parent
    doc = (root / "docs" / "CONTROL_MAPPING.md").read_text(encoding="utf-8")
    corpora = {
        "safety": {c.id for c in safety_corpus.CASES},
        "adversarial": {c.id for c in adversarial_corpus.CASES},
        "credentials": {c.id for c in credential_corpus.CASES},
        "packages": {c.id for c in package_corpus.CASES},
    }
    tests = re.findall(r"`(test_[a-z0-9_]+)::(test_[a-z0-9_]+)`", doc)
    cases = re.findall(r"`(safety|adversarial|credentials|packages):([a-z0-9-]+)`", doc)
    assert tests and cases, "the mapping cites no evidence at all"
    for module, name in tests:
        source = (root / "tests" / f"{module}.py").read_text(encoding="utf-8")
        assert f"def {name}(" in source, f"{module}::{name} does not exist"
    for suite, case in cases:
        assert case in corpora[suite], f"{suite}:{case} does not exist"
