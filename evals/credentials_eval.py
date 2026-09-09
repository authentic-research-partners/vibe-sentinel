"""What the credentials gate finds, and what it decides. Measured.

The gate is two stages and they are measured apart, because they fail
differently. The pattern stage has to be generous — a candidate it never
raises is one nobody ever gets to judge — so what matters there is
recall, and the price of it is a false-alarm rate somebody has to live
with. The model stage is where "would this string open something" is
actually answered, and the corpus is built around the pairs no regex can
separate: a documented example key with the real prefix, a model name
shaped exactly like a typed password, a joke password in a test.

One thing this eval refuses to do quietly: the gate will not send an
excerpt to an endpoint that is not on this machine, and neither will
this. A non-loopback endpoint stops the judged tier and says so, rather
than measuring nothing and reporting a clean sheet.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from evals.corpus import credentials as corpus
from evals.metrics import Counts, Matrix, Measurement, Outcome, Suite
from vibe_sentinel import credentials
from vibe_sentinel.config import SentinelConfig
from vibe_sentinel.exceptions import LLMConnectionError

#: Worst first. A file whose candidates were judged differently takes the
#: most serious of them, which is what the gate itself does when it
#: decides whether to fail.
_ORDER = ("real", "unclear", "placeholder", "pinned", "unreviewed")


def plant_scene(root: Path) -> None:
    """Write every case's file into one tree, as one project."""
    for case in corpus.CASES:
        path = root / case.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(case.content, encoding="utf-8")


def _worst(verdicts: list[str]) -> str:
    for verdict in _ORDER:
        if verdict in verdicts:
            return verdict
    return "unreviewed"


def measure_detection(scan: credentials.Scan) -> tuple[Measurement, Measurement]:
    """The mechanical stage: what was flagged, and under which rule."""
    found: dict[str, list[str]] = {}
    for candidate in scan.candidates:
        found.setdefault(candidate.path, []).append(candidate.rule)

    detection = Counts()
    detection_out: list[Outcome] = []
    rule_out: list[Outcome] = []
    for case in corpus.CASES:
        rules = sorted(found.get(case.path, []))
        detection.record(case.detected, bool(rules))
        detection_out.append(
            Outcome(
                case=case.id,
                expected="flagged" if case.detected else "clean",
                observed="flagged" if rules else "clean",
                ok=bool(rules) == case.detected,
                detail=", ".join(rules),
            )
        )
        for rule in case.rules:
            hit = rule in rules
            rule_out.append(
                Outcome(
                    case=f"{case.id}/{rule}",
                    expected="matched",
                    observed="matched" if hit else "missed",
                    ok=hit,
                    detail=""
                    if hit
                    else f"matched instead: {', '.join(rules) or 'nothing'}",
                )
            )

    return (
        Measurement(
            id="pattern-detection",
            question=(
                "Of the files holding something worth a question, how many "
                "produced a candidate — and how many ordinary files produced "
                "one anyway?"
            ),
            counts=detection,
            outcomes=detection_out,
            note=(
                "recall is the one that matters here: a candidate never "
                "raised is one the model never judges"
            ),
        ),
        Measurement(
            id="rule-named",
            question=(
                "Was each file flagged by the rule whose question fits it? "
                "The rule decides what the model is asked."
            ),
            outcomes=rule_out,
        ),
    )


async def measure_adjudication(
    scan: credentials.Scan,
    secrets: tuple[credentials.Secret, ...],
    policy: credentials.Policy,
    config: SentinelConfig,
) -> Measurement:
    """The model stage: which candidates would actually open something."""
    matrix = Matrix()
    outcomes: list[Outcome] = []
    unanswered = 0

    try:
        findings = await credentials.adjudicate(scan, secrets, policy, config)
    except LLMConnectionError as e:
        return Measurement(
            id="model-verdict",
            question=(
                "Of the candidates the patterns raised, which would open "
                "something and which are examples, defaults and fixtures?"
            ),
            tier="judged",
            note=f"not run — {e}",
        )

    decided: dict[str, list[str]] = {}
    reasons: dict[str, str] = {}
    for judgement in findings.judgements:
        path = judgement.candidate.path
        decided.setdefault(path, []).append(judgement.verdict)
        if judgement.reason and path not in reasons:
            reasons[path] = judgement.reason

    for case in corpus.CASES:
        if not case.verdict:
            continue
        verdict = _worst(decided.get(case.path, []))
        if verdict in ("unreviewed", "pinned") or case.path not in decided:
            unanswered += 1
            outcomes.append(
                Outcome(
                    case=case.id,
                    expected=case.verdict,
                    observed=verdict,
                    ok=False,
                    detail=(
                        "no verdict from the model — not counted in the accuracy above"
                    ),
                )
            )
            continue
        matrix.record(case.verdict, verdict)
        outcomes.append(
            Outcome(
                case=case.id,
                expected=case.verdict,
                observed=verdict,
                ok=case.accepts(verdict),
                detail=reasons.get(case.path, "")[:160],
            )
        )

    missed = matrix.count("real", "placeholder")
    alarms = matrix.count("placeholder", "real")
    return Measurement(
        id="model-verdict",
        question=(
            "Of the candidates the patterns raised, which would open "
            "something and which are examples, defaults and fixtures?"
        ),
        tier="judged",
        matrix=matrix,
        outcomes=outcomes,
        unanswered=unanswered,
        note=(
            f"the two cells that matter: {missed} live value(s) called a "
            f"placeholder, {alarms} placeholder(s) called live"
        ),
    )


async def run(config: SentinelConfig, mechanical_only: bool = False) -> Suite:
    """Everything the credentials corpus measures, in one suite."""
    started = time.monotonic()
    secrets = credentials.builtin_secrets()
    policy = credentials.Policy()
    note = ""
    used = False

    with tempfile.TemporaryDirectory(prefix="vs-eval-credentials-") as tmp:
        root = Path(tmp)
        plant_scene(root)
        scan = credentials.search(root, secrets, policy)
        measurements = list(measure_detection(scan))
        if not mechanical_only:
            judged = await measure_adjudication(scan, secrets, policy, config)
            measurements.append(judged)
            used = bool(judged.matrix and judged.matrix.total)
            if not used:
                note = judged.note or (
                    "no candidate came back with a verdict — is a backend "
                    "running? vibe-sentinel backend status"
                )
        else:
            note = "--mechanical: the judged tier was not run"
            measurements.append(
                Measurement(
                    id="model-verdict",
                    question=(
                        "Of the candidates the patterns raised, which would "
                        "open something?"
                    ),
                    tier="judged",
                    note="not run under --mechanical",
                )
            )

    return Suite(
        id="credentials",
        title="secrets at rest, and which of them are real",
        measurements=measurements,
        used_model=used,
        model=config.llm_model if not mechanical_only else "",
        endpoint=config.llm_endpoint if not mechanical_only else "",
        duration_s=time.monotonic() - started,
        note=note,
    )
