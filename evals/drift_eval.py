"""What a lens makes of a drift report, measured against known answers.

Three questions, and the second is the one that could not be asked any
other way. ``analyze._apply`` silently drops a rating that names a change
nobody reported — which is what keeps the drift set reproducible, and
also what hides how often the model tries. Asked directly, this counts
it. The rate is the argument for the boundary being there at all.

The severity question needs both halves of the corpus to mean anything.
A model that answers ``high`` to everything scores perfectly on the
reorganisation report and disastrously on the ordinary-editing one; a
model that answers ``info`` to everything does the reverse. Neither is
visible from one report.

The third is arithmetic that used to be a guess.
``analyze.answer_budget`` sizes the answer from the number of changes,
and the failure it was written for is on record: four lenses answered
about a 21-change report in 1322, 1432, 1762 and 2225 tokens, and the
widest overran a 2048-token ceiling and came back as half an answer that
parsed. So each case here records what its answer actually cost against
what it was given.
"""

from __future__ import annotations

import asyncio
import json
import time

from loguru import logger

from evals.cases import DriftCase, change_key
from evals.corpus import declared
from evals.corpus import drift as corpus
from evals.metrics import Matrix, Measurement, Outcome, Suite
from vibe_sentinel.analyze import answer_budget, build_context, build_question
from vibe_sentinel.config import SentinelConfig
from vibe_sentinel.exceptions import LLMConnectionError
from vibe_sentinel.json_schema import clip_to_bounds, unbounded_schema
from vibe_sentinel.llm import llm_query
from vibe_sentinel.schemas import ChangeAssessment, DriftAnalysis

_SCHEMA = unbounded_schema(DriftAnalysis)

#: The conversion ``analyze.answer_budget`` reasons in. Restated rather
#: than imported: it is a private constant there, and an eval that
#: depended on the internals of the thing it measures would agree with
#: them by construction.
_CHARS_PER_TOKEN = 3


def _resolve(rating: ChangeAssessment, case: DriftCase) -> tuple[str, str] | None:
    """Which change this rating lands on, by the rule ``_apply`` uses.

    The pair first, since that is the identity the changes table stores.
    A bare key is enough where it names exactly one change and ambiguous
    where it names several — and ambiguous is dropped rather than
    guessed, because a mechanical severity is a worse answer than the
    right one and a much better answer than the wrong one.
    """
    pairs = [(c.probe_id, c.key) for c in case.report.changes]
    if (rating.probe_id, rating.key) in pairs:
        return (rating.probe_id, rating.key)
    sharing = [pair for pair in pairs if pair[1] == rating.key]
    return sharing[0] if len(sharing) == 1 else None


async def _ask(
    case: DriftCase, config: SentinelConfig
) -> tuple[DriftCase, DriftAnalysis | None, int, int]:
    """One report, one lens. Returns the answer, its size, and its budget."""
    budget = answer_budget(len(case.report.changes), config.max_tokens)
    tuned = config.model_copy(update={"max_tokens": budget})
    raw = await llm_query(
        build_context(case.report, case.snapshot, case.guidance),
        build_question(case.lens),
        _SCHEMA,
        f"eval-drift-{case.id}",
        config=tuned,
    )
    if raw is None:
        return case, None, 0, budget
    spent = len(json.dumps(raw)) // _CHARS_PER_TOKEN
    try:
        return (
            case,
            DriftAnalysis.model_validate(clip_to_bounds(DriftAnalysis, raw)),
            spent,
            budget,
        )
    except Exception as e:  # noqa: BLE001 - an unusable answer is a result
        logger.debug("drift eval ({}): unusable answer ({})", case.id, e)
        return case, None, spent, budget


def _declared(config: SentinelConfig) -> tuple[tuple[DriftCase, ...], Measurement]:
    """This project's own cases, and the measurement that says they load.

    Loading is itself a check worth reporting: every band, every probe id
    and every watch is validated on the way in, so a case that could
    never be measured fails here rather than scoring well. That runs
    without a model, which is why it is the drift suite's one mechanical
    measurement.
    """
    cases = declared.load_cases(config.project_dir, config.drift_guidance)
    return cases, Measurement(
        id="declared-cases",
        question=(
            "Do this project's own [[lens.case]] declarations load — every "
            "band a severity, every probe one that runs, every case moving "
            "something its lens watches?"
        ),
        outcomes=[
            Outcome(
                case=case.id,
                expected="loads",
                observed="loads",
                ok=True,
                detail=f"{len(case.bands)} banded change(s)",
            )
            for case in cases
        ],
        note=(
            f"{len(cases)} case(s) declared beside this project's lenses"
            if cases
            else (
                "none declared — only the shipped corpus ran. A [[probe]] and "
                "the [[lens]] that reads it are two thirds of a measurement; "
                "see docs/adding-a-probe.md for the third"
            )
        ),
    )


async def run(config: SentinelConfig, mechanical_only: bool = False) -> Suite:
    """Everything the drift corpora measure, in one suite.

    Two corpora, and the second is the one that matters to anybody but
    this project: ``evals/corpus/drift.py`` measures the shipped lens
    against shipped probes, and ``[[lens.case]]`` in the project's own
    config measures its own questions about its own measurements.
    """
    started = time.monotonic()
    project_cases, loaded = _declared(config)
    cases = (*corpus.CASES, *project_cases)
    band_out: list[Outcome] = []
    target = Matrix()
    target_out: list[Outcome] = []
    budget_out: list[Outcome] = []
    unanswered = 0
    over_budget: list[str] = []

    if mechanical_only:
        return Suite(
            id="drift",
            title="what a lens makes of a report",
            measurements=[
                loaded,
                Measurement(
                    id="severity-band",
                    question="Does a severity land in the band a person would accept?",
                    tier="judged",
                    note="not run: rating a report needs the model",
                ),
            ],
            duration_s=time.monotonic() - started,
            note=(
                "--mechanical: the declared cases were validated, and nothing was rated"
            ),
        )

    limit = asyncio.Semaphore(max(1, config.drift_concurrency))

    async def one(case: DriftCase) -> tuple[DriftCase, DriftAnalysis | None, int, int]:
        async with limit:
            return await _ask(case, config)

    # ``return_exceptions=True`` means nothing raises out of here — a
    # backend that is not running comes back as an item, not as a throw,
    # and is reported as the reason rather than swallowed as silence.
    results = await asyncio.gather(
        *(one(case) for case in cases), return_exceptions=True
    )
    unreachable = next(
        (str(item) for item in results if isinstance(item, LLMConnectionError)), ""
    )

    answered = 0
    for item in results:
        if isinstance(item, BaseException) or not isinstance(item, tuple):
            logger.warning("drift eval: a case failed ({})", item)
            unanswered += 1
            continue
        case, analysis, spent, budget = item
        if analysis is None:
            unanswered += 1
            for band in case.bands:
                band_out.append(
                    Outcome(
                        case=f"{case.id}/{change_key(*band.pair)}",
                        expected="|".join(band.accept),
                        observed="unanswered",
                        ok=False,
                        detail="the model did not answer about this report",
                    )
                )
            continue

        answered += 1
        landed: dict[tuple[str, str], ChangeAssessment] = {}
        for rating in analysis.changes:
            pair = _resolve(rating, case)
            hit = pair is not None
            landed_label = "a change in the report" if hit else "a key nobody measured"
            target.record("a change in the report", landed_label)
            target_out.append(
                Outcome(
                    case=f"{case.id}/{change_key(rating.probe_id, rating.key)}",
                    expected="a change in the report",
                    observed=landed_label,
                    ok=hit,
                    detail="" if hit else "the rating names something nobody measured",
                )
            )
            if pair is not None:
                landed[pair] = rating

        for band in case.bands:
            landed_on = landed.get(band.pair)
            severity = landed_on.severity if landed_on else "(not rated)"
            band_out.append(
                Outcome(
                    case=f"{case.id}/{change_key(*band.pair)}",
                    expected="|".join(band.accept),
                    observed=severity,
                    ok=severity in band.accept,
                    detail=(
                        landed_on.note[:140]
                        if landed_on
                        else "kept its mechanical severity"
                    ),
                )
            )

        fits = spent <= budget
        if not fits:
            over_budget.append(case.id)
        budget_out.append(
            Outcome(
                case=case.id,
                expected=f"<= {budget} tokens",
                observed=f"~{spent} tokens",
                ok=fits,
                detail=f"{len(case.report.changes)} change(s) in the report",
            )
        )

    headroom = (
        min(
            (
                int(o.expected.split()[1]) - int(o.observed.strip("~").split()[0])
                for o in budget_out
            ),
            default=0,
        )
        if budget_out
        else 0
    )

    measurements = [
        loaded,
        Measurement(
            id="severity-band",
            question=(
                "Does each severity land in the band a person would accept — "
                "including the report where the right answer is that nothing "
                "here matters?"
            ),
            tier="judged",
            outcomes=band_out,
            unanswered=unanswered,
        ),
        Measurement(
            id="stays-in-the-change-set",
            question=(
                "Does every rating name a change the comparison actually "
                "found? A rating that names anything else is dropped, and "
                "this is how often that happens."
            ),
            tier="judged",
            matrix=target,
            outcomes=target_out,
            note=(
                "the model may only rate what compare() found — an invented "
                "key is silently discarded in a real scan, so this is the "
                "only place the rate is visible"
            ),
        ),
        Measurement(
            id="answer-budget",
            question=("Did each answer fit the budget answer_budget() sized for it?"),
            tier="judged",
            outcomes=budget_out,
            note=(
                f"tightest headroom {headroom} tokens"
                + (f"; over budget: {', '.join(over_budget)}" if over_budget else "")
            ),
        ),
    ]

    return Suite(
        id="drift",
        title="what a lens makes of a report",
        measurements=measurements,
        used_model=answered > 0,
        model=config.llm_model,
        endpoint=config.llm_endpoint,
        duration_s=time.monotonic() - started,
        note=(
            ""
            if answered
            else (
                f"the backend was not reachable — {unreachable}"
                if unreachable
                else (
                    "no report came back with an answer — is a backend "
                    "running? vibe-sentinel backend status"
                )
            )
        ),
    )
