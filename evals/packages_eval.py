"""Two installed names one edit apart: found, and then told apart.

The pairing is arithmetic — an edit distance with two documented
exceptions — so what it needs measuring for is the exceptions. A trailing
digit is how a project ships a rewrite, and the pairing declines to
report it; four-character names collide constantly, and the pairing
declines to look. Both are deliberate, both are invisible in a corpus of
squats only, and both would fail silently if the rule went away.

The adjudication is the one provenance check that is a judgement rather
than a fact, and the facts it is given are the whole of what it has: what
each package says it is, who installed it, whether anything asked for it,
whether this project's source imports it. So the inventory is synthetic
and complete — a real environment would put whatever it happens to hold
in front of the model, and the answer would then be about this machine
rather than about the tool.
"""

from __future__ import annotations

import time

from evals.cases import NearMissCase, PackageFacts
from evals.corpus import packages as corpus
from evals.metrics import Counts, Matrix, Measurement, Outcome, Suite
from vibe_sentinel import packages
from vibe_sentinel.config import SentinelConfig
from vibe_sentinel.exceptions import LLMConnectionError


def _installed(facts: PackageFacts) -> packages.Installed:
    return packages.Installed(
        name=facts.name,
        version=facts.version,
        installer=facts.installer,
        provenance_recorded=bool(facts.installer),
        summary=facts.summary,
    )


def build_inventory() -> packages.Inventory:
    """Every corpus name as one environment, with its facts attached.

    ``requires`` is filled from the other direction: a case says who
    requires a name, and ``required_by`` — which is what the prompt
    quotes — is derived by inverting that, exactly as the audit does.
    """
    facts = [f for case in corpus.CASES for f in case.facts]
    installed = {f.name: _installed(f) for f in facts}
    for f in facts:
        for requirer in f.required_by:
            if requirer not in installed:
                installed[requirer] = packages.Installed(
                    name=requirer, version="1.0.0", installer="uv"
                )
            current = installed[requirer]
            installed[requirer] = current.model_copy(
                update={"requires": (*current.requires, f.name)}
            )

    declared = tuple(
        packages.Requirement(
            name=f.name, raw=f.name, specifier="", marker="", group="project"
        )
        for f in facts
        if f.declared
    )
    imports = tuple(
        packages.SourceImport(module=f.name, sites=("src/app.py:1",))
        for f in facts
        if f.imported
    )
    return packages.Inventory(
        environment=packages.environment(),
        declared=declared,
        installed=installed,
        closure=packages.requirement_closure({r.name for r in declared}, installed),
        imports=imports,
        first_party=frozenset(),
        project="scene",
    )


def _finding(case: NearMissCase) -> packages.Finding:
    """The finding the audit would make for this pair, made the same way."""
    first, second = sorted(case.pair)
    return packages.Finding(
        kind="near-miss",
        name=case.suspect or second,
        detail=f"{first!r} and {second!r} are one edit apart and both installed",
        remediation=(
            "Confirm both are intended. One character between two installed "
            "names is the typosquat shape, and it survives review because "
            "the wrong one reads correctly."
        ),
        evidence=(first, second),
    )


def measure_pairing() -> Measurement:
    """Does the mechanical stage report the pairs, and only those pairs?"""
    found = {tuple(sorted(pair)) for pair in packages.near_misses(corpus.all_names())}
    labelled = {tuple(sorted(case.pair)): case for case in corpus.CASES}

    counts = Counts()
    outcomes: list[Outcome] = []
    for case in corpus.CASES:
        hit = tuple(sorted(case.pair)) in found
        counts.record(case.paired, hit)
        outcomes.append(
            Outcome(
                case=case.id,
                expected="paired" if case.paired else "not paired",
                observed="paired" if hit else "not paired",
                ok=hit == case.paired,
                detail=" / ".join(case.pair),
            )
        )
    for pair in sorted(found - set(labelled)):
        counts.record(False, True)
        outcomes.append(
            Outcome(
                case=f"unlabelled:{pair[0]}+{pair[1]}",
                expected="not paired",
                observed="paired",
                ok=False,
                detail="a pair the corpus does not label — every one is a question somebody has to answer",
            )
        )

    return Measurement(
        id="pairing",
        question=(
            "Of the name pairs that are worth asking about, how many were "
            "raised — and how many pairs nobody needs to see came with them?"
        ),
        counts=counts,
        outcomes=outcomes,
        note=(
            "the two `not paired` cases are the documented exceptions: a "
            "trailing digit is a rewrite, and short names are below the floor"
        ),
    )


async def measure_adjudication(config: SentinelConfig) -> Measurement:
    """Two real packages, or one wearing the other's name?"""
    judged = [case for case in corpus.CASES if case.paired and case.verdict]
    inventory = build_inventory()
    policy = packages.Policy()
    findings = [_finding(case) for case in judged]

    matrix = Matrix()
    outcomes: list[Outcome] = []
    unanswered = 0
    try:
        results = await packages.review_near_misses(findings, inventory, policy, config)
    except LLMConnectionError as e:
        return Measurement(
            id="model-verdict",
            question=(
                "Given what each package says it is and who asked for it, "
                "are these two packages or one imposter?"
            ),
            tier="judged",
            note=f"not run — {e}",
        )

    by_name = {j.finding.name: j for j in results}
    for case in judged:
        judgement = by_name.get(_finding(case).name)
        # `standing` and `pinned` are not the model's answers: the first
        # means the answer was unusable and the finding was left as the
        # audit made it, the second that a pin settled it beforehand.
        # Neither may be scored as a verdict somebody gave.
        settled = judgement is not None and judgement.verdict in (
            "distinct",
            "typosquat",
            "unclear",
        )
        if judgement is None or not settled:
            unanswered += 1
            outcomes.append(
                Outcome(
                    case=case.id,
                    expected=case.verdict,
                    observed="unreviewed",
                    ok=False,
                    detail="no verdict from the model — not counted above",
                )
            )
            continue
        matrix.record(case.verdict, judgement.verdict)
        outcomes.append(
            Outcome(
                case=case.id,
                expected=case.verdict,
                observed=judgement.verdict,
                ok=case.accepts(judgement.verdict),
                detail=judgement.reason[:160],
            )
        )

    missed = matrix.count("typosquat", "distinct")
    alarms = matrix.count("distinct", "typosquat")
    return Measurement(
        id="model-verdict",
        question=(
            "Given what each package says it is and who asked for it, are "
            "these two packages or one imposter?"
        ),
        tier="judged",
        matrix=matrix,
        outcomes=outcomes,
        unanswered=unanswered,
        note=(
            f"the two cells that matter: {missed} squat(s) cleared as "
            f"distinct, {alarms} real package(s) called a squat"
        ),
    )


async def run(config: SentinelConfig, mechanical_only: bool = False) -> Suite:
    """Everything the near-miss corpus measures, in one suite."""
    started = time.monotonic()
    measurements = [measure_pairing()]
    used = False
    note = ""

    if not mechanical_only:
        judged = await measure_adjudication(config)
        measurements.append(judged)
        used = bool(judged.matrix and judged.matrix.total)
        if not used:
            note = judged.note or (
                "no pair came back with a verdict — is a backend running? "
                "vibe-sentinel backend status"
            )
    else:
        note = "--mechanical: the judged tier was not run"
        measurements.append(
            Measurement(
                id="model-verdict",
                question="Are these two packages, or one wearing the other's name?",
                tier="judged",
                note="not run under --mechanical",
            )
        )

    return Suite(
        id="packages",
        title="names one edit apart, and which pair is a squat",
        measurements=measurements,
        used_model=used,
        model=config.llm_model if not mechanical_only else "",
        endpoint=config.llm_endpoint if not mechanical_only else "",
        duration_s=time.monotonic() - started,
        note=note,
    )
