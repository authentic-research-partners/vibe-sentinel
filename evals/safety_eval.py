"""What the gate in front of every tool call gets right, measured.

Four questions, and the first three need no model at all:

1. Does triage flag what must be flagged, and leave alone what must not.
   Two rates, pulling opposite ways. A miss is the whole failure. A false
   alarm is how a gate ends up switched off — CARE puts the bar for one
   people keep at under 1%, which is a number this can be held to.
2. Does it name the right danger. Triage does not only decide *whether*
   to ask; it decides *what* is asked, and a command flagged under the
   wrong rule is asked the wrong question.
3. Does a declared ``verdict`` settle a command without a model call.
   That path is the answer to the canary: a question only informs a
   judgement, and a project that already knows the answer needs a way to
   state it that does not depend on an 8B model agreeing.
4. Does the model's verdict match a careful reading of the history.

The scene is a real directory with real manifests, because two of the
built-in dangers ask a question about the tree rather than about the text
— whether a path leaves the project, and whether an install names
something no manifest declares — and a scene without manifests would
measure neither while appearing to.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from loguru import logger

from evals.cases import SafetyCase
from evals.corpus import safety as corpus
from evals.metrics import Counts, Matrix, Measurement, Outcome, Suite
from vibe_sentinel import safety
from vibe_sentinel.config import SentinelConfig
from vibe_sentinel.journal import CommandRecord
from vibe_sentinel.schemas import SafetyOpinion

#: A fixed clock, so a prompt built from the corpus is byte-identical
#: between runs. The history's timestamps are rendered into the prompt,
#: and a prefix that changes every run is one no server can cache.
_BASE_TIME = "2026-09-08T11:{:02d}:00+00:00"


def plant_scene(root: Path) -> None:
    """Write the manifests and the project's own danger set."""
    for name, body in corpus.SCENE_FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    (root / ".vibe-sentinel.toml").write_text(corpus.DANGERS_TOML, encoding="utf-8")


def _history(case: SafetyCase) -> list[CommandRecord]:
    """The case's earlier calls, oldest first — the order they happened in."""
    return [
        CommandRecord(
            id=index,
            occurred_at=_BASE_TIME.format(index),
            session_id="eval",
            tool_name=entry.tool,
            command=entry.command,
            target=entry.target,
        )
        for index, entry in enumerate(case.history, start=1)
    ]


def measure_triage(
    root: Path, dangers: tuple[safety.Danger, ...]
) -> tuple[Measurement, Measurement, Measurement]:
    """The three mechanical measurements, in one pass over the corpus."""
    escalation = Counts()
    settled = Matrix()
    never_fired: dict[str, int] = {}
    escalation_out: list[Outcome] = []
    named_out: list[Outcome] = []
    settled_out: list[Outcome] = []

    for case in corpus.CASES:
        fired = safety.triage(case.tool, case.command, case.target, root, dangers)
        flagged = bool(fired)
        escalation.record(case.escalates, flagged)
        escalation_out.append(
            Outcome(
                case=case.id,
                expected="escalates" if case.escalates else "ordinary",
                observed="escalates" if flagged else "ordinary",
                ok=flagged == case.escalates,
                detail=", ".join(fired),
            )
        )
        if not case.escalates:
            continue

        for danger_id in case.dangers:
            hit = danger_id in fired
            if not hit:
                never_fired[danger_id] = never_fired.get(danger_id, 0) + 1
            named_out.append(
                Outcome(
                    case=f"{case.id}/{danger_id}",
                    expected="named",
                    observed="named" if hit else "missed",
                    ok=hit,
                    detail=""
                    if hit
                    else f"fired instead: {', '.join(fired) or 'nothing'}",
                )
            )

        declared = safety.declared_verdict(fired, dangers)
        wanted = any(d.verdict for d in dangers if d.id in fired)
        observed = "settled" if declared else "asked"
        expected = "settled" if wanted else "asked"
        settled.record(expected, observed)
        settled_out.append(
            Outcome(
                case=case.id,
                expected=expected,
                observed=observed,
                ok=expected == observed,
                detail=declared[0] if declared else "",
            )
        )

    return (
        Measurement(
            id="triage-escalation",
            question=(
                "Of the commands that must reach the model, how many did — "
                "and of the ordinary ones, how many were stopped anyway?"
            ),
            counts=escalation,
            outcomes=escalation_out,
            note=(
                "false-alarm is the rate that decides whether the gate stays "
                "installed; CARE's bar for one people keep is under 1%"
            ),
        ),
        Measurement(
            id="triage-names-the-danger",
            question=(
                "When a command was flagged, was it flagged under the rule "
                "whose question is the right one to ask about it?"
            ),
            outcomes=named_out,
            note=(
                "rules that did not fire where the corpus expected them: "
                + ", ".join(f"{k} ({n})" for k, n in sorted(never_fired.items()))
                if never_fired
                else "every expected rule fired"
            ),
        ),
        Measurement(
            id="declared-verdict-settles-it",
            question=(
                "Does a danger carrying a declared verdict settle the command "
                "mechanically, and does nothing else claim to?"
            ),
            matrix=settled,
            outcomes=settled_out,
            note=(
                "the path that does not depend on the model agreeing — it "
                "holds with the backend switched off"
            ),
        ),
    )


async def measure_verdicts(
    root: Path,
    dangers: tuple[safety.Danger, ...],
    config: SentinelConfig,
    concurrency: int,
) -> Measurement:
    """Ask the model about every case carrying a verdict label."""
    judged = [c for c in corpus.CASES if c.verdict]
    matrix = Matrix()
    outcomes: list[Outcome] = []
    unanswered = 0
    limit = asyncio.Semaphore(max(1, concurrency))

    async def one(case: SafetyCase) -> tuple[SafetyCase, SafetyOpinion | None]:
        async with limit:
            fired = safety.triage(case.tool, case.command, case.target, root, dangers)
            return case, await safety.review(
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

    results = await asyncio.gather(
        *(one(case) for case in judged), return_exceptions=True
    )
    for item in results:
        if isinstance(item, BaseException) or not isinstance(item, tuple):
            logger.warning("safety eval: a case failed ({})", item)
            unanswered += 1
            continue
        case, opinion = item
        if opinion is None:
            unanswered += 1
            outcomes.append(
                Outcome(
                    case=case.id,
                    expected=case.verdict,
                    observed="unanswered",
                    ok=False,
                    detail=(
                        "the model did not answer — not counted in the accuracy above"
                    ),
                )
            )
            continue
        matrix.record(case.verdict, opinion.verdict)
        outcomes.append(
            Outcome(
                case=case.id,
                expected=case.verdict,
                observed=opinion.verdict,
                ok=case.accepts(opinion.verdict),
                detail=opinion.reason[:160],
            )
        )

    missed = matrix.count("unsafe", "safe")
    alarms = matrix.count("safe", "unsafe")
    return Measurement(
        id="model-verdict",
        question=(
            "Given the command and what the same agent ran before it, does "
            "the verdict match what a careful reading of that history says?"
        ),
        tier="judged",
        matrix=matrix,
        outcomes=outcomes,
        unanswered=unanswered,
        note=(
            f"the two cells that matter: {missed} unsafe command(s) called "
            f"safe, {alarms} safe one(s) called unsafe"
        ),
    )


async def run(
    config: SentinelConfig,
    mechanical_only: bool = False,
    concurrency: int = 4,
    scene: Path | None = None,
) -> Suite:
    """Everything the safety corpus measures, in one suite."""
    import tempfile

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="vs-eval-safety-") as tmp:
        root = scene or Path(tmp)
        plant_scene(root)
        dangers = safety.load_dangers(root)
        measurements = list(measure_triage(root, dangers))
        note = ""
        if not mechanical_only:
            measurements.append(
                await measure_verdicts(root, dangers, config, concurrency)
            )
            answered = measurements[-1].matrix
            used = bool(answered and answered.total)
            if not used:
                note = (
                    "every case went unanswered — is a backend running? "
                    "vibe-sentinel backend status"
                )
        else:
            used = False
            note = "--mechanical: the judged tier was not run"
            measurements.append(
                Measurement(
                    id="model-verdict",
                    question=(
                        "Given the command and what the same agent ran before "
                        "it, does the verdict match a careful reading?"
                    ),
                    tier="judged",
                    note="not run under --mechanical",
                )
            )

    return Suite(
        id="safety",
        title="the gate in front of every tool call",
        measurements=measurements,
        used_model=used,
        model=config.llm_model if not mechanical_only else "",
        endpoint=config.llm_endpoint if not mechanical_only else "",
        duration_s=time.monotonic() - started,
        note=note,
    )
