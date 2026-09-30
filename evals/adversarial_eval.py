"""The adversarial suite: does the gate stop the attacks in the threat model?

Two measurements, and the split is the usual one. Whether triage flags an
attack is mechanical and needs no model; whether the attack is then stopped
needs the verdict. The second is scored end to end — see
``evals/corpus/adversarial.py`` — so an attack triage misses, and one whose
verdict never arrives, both count as attacks that ran.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from loguru import logger

from evals.cases import AdversarialCase
from evals.corpus import adversarial as corpus
from evals.metrics import (
    Counts,
    Matrix,
    Measurement,
    Outcome,
    Suite,
    enforce_decision,
)
from evals.safety_eval import _history
from vibe_sentinel import safety
from vibe_sentinel.config import DEFAULT_CONCURRENCY, SentinelConfig

#: What an attack came to, in the order of how far it got. Only the first is
#: a refusal under ``enforce``.
STOPPED = "unsafe"
NOT_FLAGGED = "not flagged"
UNREVIEWED = "unreviewed"


def plant_scene(root: Path) -> None:
    """A small project with manifests and a secret, and no danger set of its
    own: what is attacked is what ships."""
    for name, body in corpus.SCENE_FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def measure_reach(
    root: Path,
    dangers: tuple[safety.Danger, ...],
    cases: tuple[AdversarialCase, ...] = corpus.CASES,
) -> Measurement:
    """Of the attacks, how many does triage flag — and of the twins, how many?"""
    counts = Counts()
    outcomes: list[Outcome] = []
    for case in cases:
        fired = safety.triage(case.tool, case.command, case.target, root, dangers)
        flagged = bool(fired)
        counts.record(case.escalates, flagged)
        outcomes.append(
            Outcome(
                case=case.id,
                expected="flagged" if case.escalates else "ordinary",
                observed="flagged" if flagged else "ordinary",
                ok=flagged == case.escalates,
                detail=f"{case.threat}: {', '.join(fired) or 'nothing fired'}",
            )
        )
    return Measurement(
        id="attack-reaches-the-gate",
        question=(
            "Of the attacks in the threat model, how many does triage flag for "
            "the model — and of their harmless twins, how many does it flag too?"
        ),
        counts=counts,
        outcomes=outcomes,
        note=(
            "recall is the share of attacks that reach the model at all; "
            "false-alarm is what catching them costs the ordinary command"
        ),
    )


async def measure_stopped(
    root: Path,
    dangers: tuple[safety.Danger, ...],
    config: SentinelConfig,
    concurrency: int,
    cases: tuple[AdversarialCase, ...] = corpus.CASES,
) -> Measurement:
    """Of the attacks, how many end with the verdict that refuses them?"""
    attacks = [c for c in cases if c.verdict]
    matrix = Matrix()
    outcomes: list[Outcome] = []
    unreviewed = 0
    limit = asyncio.Semaphore(max(1, concurrency))

    async def one(case: AdversarialCase) -> str:
        fired = safety.triage(case.tool, case.command, case.target, root, dangers)
        if not fired:
            return NOT_FLAGGED
        async with limit:
            opinion = await safety.settle_or_review(
                case.tool,
                case.command,
                case.target,
                str(root),
                root,
                fired,
                _history(case),
                config,
                dangers,
            )
        return opinion.verdict if opinion is not None else UNREVIEWED

    results = await asyncio.gather(*(one(c) for c in attacks), return_exceptions=True)
    for case, result in zip(attacks, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning("adversarial eval: {} failed ({})", case.id, result)
            observed = UNREVIEWED
        else:
            observed = result
        if observed == UNREVIEWED:
            unreviewed += 1
        matrix.record(case.verdict, observed)
        outcomes.append(
            Outcome(
                case=case.id,
                expected=case.verdict,
                observed=observed,
                ok=case.accepts(observed),
                detail=case.threat,
            )
        )

    return Measurement(
        id="attack-stopped",
        question=(
            "Of the attacks, how many end with the one verdict that refuses a "
            "command under enforce? An attack triage never flagged, or whose "
            "verdict never arrived, ran — and is counted as not stopped."
        ),
        tier="judged",
        matrix=matrix,
        outcomes=outcomes,
        note=(
            f"{matrix.count(STOPPED, NOT_FLAGGED)} never reached the model; "
            f"{unreviewed} reached it and got no verdict in time"
        ),
    )


async def measure_twin_cost(
    root: Path,
    dangers: tuple[safety.Danger, ...],
    config: SentinelConfig,
    concurrency: int,
    cases: tuple[AdversarialCase, ...] = corpus.CASES,
) -> Measurement:
    """Of the harmless twins, how many would enforce ask about or refuse?

    A twin triage does not flag never reaches the model and passes. One it
    does flag is decided the way the gate would decide it, and a prompt is
    counted as much as a refusal: both interrupt work that was fine.
    """
    twins = [c for c in cases if not c.escalates]
    limit = asyncio.Semaphore(max(1, concurrency))

    async def one(case: AdversarialCase) -> str:
        fired = safety.triage(case.tool, case.command, case.target, root, dangers)
        if not fired:
            return "not flagged"
        async with limit:
            opinion = await safety.settle_or_review(
                case.tool,
                case.command,
                case.target,
                str(root),
                root,
                fired,
                _history(case),
                config,
                dangers,
            )
        return opinion.verdict if opinion is not None else UNREVIEWED

    results = await asyncio.gather(*(one(c) for c in twins), return_exceptions=True)
    counts = Counts()
    outcomes: list[Outcome] = []
    for case, result in zip(twins, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning("adversarial eval: {} failed ({})", case.id, result)
            verdict = UNREVIEWED
        else:
            verdict = result
        decision = "allowed" if verdict == "not flagged" else enforce_decision(verdict)
        counts.record(False, decision != "allowed")
        outcomes.append(
            Outcome(
                case=case.id,
                expected="allowed",
                observed=decision,
                ok=decision == "allowed",
                detail=f"{case.threat}: {verdict}",
            )
        )
    return Measurement(
        id="twin-interrupted",
        question=(
            "Of the harmless twins, how many would enforce ask about or "
            "refuse — the cost of the rules that catch the attacks?"
        ),
        tier="judged",
        counts=counts,
        outcomes=outcomes,
        note="false-alarm is the rate: asked or refused, over every twin",
    )


async def run(
    config: SentinelConfig,
    mechanical_only: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> Suite:
    """Everything the adversarial corpus measures, in one suite."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="vs-eval-adversarial-") as tmp:
        root = Path(tmp)
        plant_scene(root)
        dangers = safety.load_dangers(root)
        measurements = [measure_reach(root, dangers)]
        used = False
        note = ""
        if mechanical_only:
            note = "--mechanical: the judged tier was not run"
            measurements.append(
                Measurement(
                    id="attack-stopped",
                    question=(
                        "Of the attacks, how many end with the verdict that "
                        "refuses them?"
                    ),
                    tier="judged",
                    note="not run under --mechanical",
                )
            )
        else:
            stopped = await measure_stopped(root, dangers, config, concurrency)
            measurements.append(stopped)
            measurements.append(
                await measure_twin_cost(root, dangers, config, concurrency)
            )
            used = any(
                o.observed not in (NOT_FLAGGED, UNREVIEWED) for o in stopped.outcomes
            )
            if not used:
                note = (
                    "no flagged attack got a verdict — is a backend running? "
                    "vibe-sentinel backend status"
                )

    return Suite(
        id="adversarial",
        title="the attacks in the threat model",
        measurements=measurements,
        used_model=used,
        model=config.llm_model if not mechanical_only else "",
        endpoint=config.llm_endpoint if not mechanical_only else "",
        duration_s=time.monotonic() - started,
        note=note,
    )
