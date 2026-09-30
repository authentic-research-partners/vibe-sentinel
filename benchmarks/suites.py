"""The three questions, and the conditions that answer each.

Each suite varies exactly one thing. That is the whole discipline here:
the fan-out suite holds the prefix and the client still, the prefix suite
holds the width and the client still, and the client suite holds the
other two. A benchmark that moved two knobs would produce a number
nobody could attribute.
"""

from __future__ import annotations

import time

import httpx
from loguru import logger

from benchmarks.harness import Condition, Suite, measure, question, report
from vibe_sentinel.config import DEFAULT_CONCURRENCY, SentinelConfig

#: Fan-out widths swept by the concurrency suite. 1 is the baseline every
#: speedup is against; 4 is what all four gates ship as their default; the
#: rest are there to find where the curve stops paying.
WIDTHS = (1, 2, 4, 8, 16)

#: Requests per pass. Sized against the fan-outs a real run produces: a
#: drift pass asks once per watched lens (a handful), a safety review once
#: per matched danger (one to three), and a credentials adjudication once
#: per unpinned candidate, which is the one that gets large — a tree with
#: no pins yet routinely flags dozens.
REQUESTS = 16

#: Width for the two suites that are not sweeping it. The shipped default,
#: so what they measure is the configuration people actually run.
DEFAULT_WIDTH = DEFAULT_CONCURRENCY


def _shared(index: int) -> tuple[str, str]:
    """Every request carries the same system message — the tool's shape."""
    return report(), question(index)


def _distinct(index: int) -> tuple[str, str]:
    """Every request carries its own. What the tool deliberately does not do."""
    return report(nonce=f"r{index:04d}"), question(index)


def _check_widths(widths: tuple[int, ...], requests: int) -> None:
    too_wide = [w for w in widths if w > requests]
    if too_wide:
        raise ValueError(
            f"width(s) {too_wide} exceed the {requests} requests in a pass, so "
            f"the semaphore would never be the limit and the number would be "
            f"about nothing.\n"
            f"  Raise the request count:  python -m benchmarks --requests {max(too_wide)}\n"
            f"  Or drop the width:        python -m benchmarks --widths "
            f"{','.join(str(w) for w in widths if w <= requests)}"
        )


async def concurrency(
    config: SentinelConfig,
    requests: int = REQUESTS,
    repeat: int = 3,
    widths: tuple[int, ...] = WIDTHS,
) -> Suite:
    """What the fan-out width is worth.

    The question ``config.DEFAULT_CONCURRENCY`` is the measured answer to,
    for one backend. A scan asks N questions and waits for all of them, so the
    metric is the wall clock of a fixed batch rather than throughput at
    saturation: what a person waits for is the last answer, not the mean.
    """
    _check_widths(widths, requests)
    suite = Suite(
        id="concurrency",
        question="What does the fan-out width buy, on this backend?",
        note=(
            f"{requests} requests per pass, one shared prefix, a fresh client "
            f"per call — the tool's own shape, with only the semaphore moving."
        ),
    )
    for width in widths:
        logger.info("concurrency: width {}", width)
        suite.measurements.append(
            await measure(
                Condition(
                    id=f"width-{width}",
                    label=f"concurrency = {width}",
                    width=width,
                    payload=_shared,
                ),
                config,
                requests,
                repeat,
            )
        )
    return suite


async def prefix(
    config: SentinelConfig,
    requests: int = REQUESTS,
    repeat: int = 3,
    width: int = DEFAULT_WIDTH,
) -> Suite:
    """What the identical prefix is worth.

    ``One question per request, not one request per command`` rests on it:
    the command and its history, or the drift report, are the system
    message and byte-identical across the fan-out, so a server that caches
    prefixes prefills them once. If that is true the two arms here differ
    by a lot, and if it is false the design is paying N prefills for a
    readability argument. The distinct arm puts its nonce at the *front*
    of the report, because a prefix cache is defeated only there.
    """
    _check_widths((width,), requests)
    suite = Suite(
        id="prefix",
        question="What does the byte-identical system message buy?",
        note=(
            f"{requests} requests per pass at width {width}. The arms differ "
            f"in one thing: whether every request's system message is the same "
            f"bytes."
        ),
    )
    for cid, label, payload in (
        ("shared", "one shared prefix (what the tool sends)", _shared),
        ("distinct", "a different prefix per request", _distinct),
    ):
        logger.info("prefix: {}", cid)
        suite.measurements.append(
            await measure(
                Condition(id=cid, label=label, width=width, payload=payload),
                config,
                requests,
                repeat,
            )
        )
    return suite


def client_construction(samples: int = 20) -> float:
    """Milliseconds to build one ``httpx.AsyncClient``. No model involved.

    The number that makes the client suite worth running: this is the cost
    ``llm_query`` pays per call today, and seeing it next to a generation
    is the only way to know whether it is worth removing.
    """
    started = time.perf_counter()
    for _ in range(samples):
        httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
    return (time.perf_counter() - started) / samples * 1000


async def client(
    config: SentinelConfig,
    requests: int = REQUESTS,
    repeat: int = 3,
    width: int = DEFAULT_WIDTH,
) -> Suite:
    """Whether reusing one HTTP client across a fan-out is worth doing.

    ``llm_query`` takes a ``client`` and builds its own when handed none,
    which is what every caller does. Constructing one is not free, so the
    optimisation suggests itself; whether it survives contact with a
    generation is what this answers.
    """
    _check_widths((width,), requests)
    build_ms = client_construction()
    suite = Suite(
        id="client",
        question="Is a client per call costing anything measurable?",
        note=(
            f"{requests} requests per pass at width {width}, one shared prefix. "
            f"Constructing one httpx.AsyncClient measured at {build_ms:.1f} ms "
            f"on this machine — the cost the fresh-client arm pays per request."
        ),
    )
    for cid, label, shared in (
        ("own", "a fresh client per call (what llm_query does)", False),
        ("shared", "one client for the whole fan-out", True),
    ):
        logger.info("client: {}", cid)
        suite.measurements.append(
            await measure(
                Condition(
                    id=cid,
                    label=label,
                    width=width,
                    payload=_shared,
                    shared_client=shared,
                ),
                config,
                requests,
                repeat,
            )
        )
    return suite


#: The truncating arm's budget, in tokens, absolute.
#:
#: A share of the other arm was the obvious parameterisation and it was
#: wrong: what forces an over-run is a budget below the answer's real size,
#: which has nothing to do with what the fitting arm was given. A quarter of
#: 1,024 truncated 45 of 48 requests; the same quarter of 2,048 truncated 3,
#: and the two arms came out indistinguishable while the guard below still
#: called it measured. The manipulation has to be pinned to the answer, so it
#: is pinned to the answer.
#:
#: 256 because the bench answer runs a few hundred tokens once ``_BRIEF`` has
#: named its fields — measured, in the run that produced this constant — so
#: 256 overruns and ``llm_query``'s single doubling to 512 clears it. Raise
#: it if a change to the question makes answers longer; the guard says so.
_TRUNCATING_TOKENS = 256

#: How much of the truncating arm has to truncate before the gap means
#: anything. Not "more than zero": three over-runs in forty-eight moves a
#: median by less than the spread between passes, so the arms differ in
#: nothing a reader could act on. Most of them, or it is not a manipulation.
_TRUNCATING_FLOOR = 0.5


async def truncation(
    config: SentinelConfig,
    requests: int = REQUESTS,
    repeat: int = 3,
    width: int = DEFAULT_WIDTH,
) -> Suite:
    """What an answer that overruns its budget costs.

    The question three gates got wrong. `safety`, `credentials` and
    `packages` each pinned ``max_tokens`` to 512 on the reasoning that the
    answer was short — but ``max_tokens`` is a ceiling, not a reservation, so
    a lower one never shortened an answer. It only decided when one was cut
    off, and a cut-off answer is not discarded: ``llm_query`` retries it at
    double the budget, so a single over-run pays a second prefill and a
    second generation for one answer.

    That was fixed on the argument alone. This puts a number on it, and
    leaves a guard behind: the arms differ in nothing but the budget, so the
    gap between them is what an over-run costs on this backend.

    **Three runs, three different guards, and no clean number.** Worth
    recording, because the obstacle is a property of the model rather than a
    lack of trying. The first run had both arms truncating, so the gap
    understated the cost. The second landed a null manipulation — three
    over-runs in forty-eight — because the budget was sized as a share of the
    other arm instead of against the answer. The third truncated 56 times and
    then lost 8 answers outright, because the doubled retry overran as well.

    There is no budget that fixes it, and the reason is the shape of the
    answer. A constrained decoder accepts whitespace between any two tokens,
    so token counts have a long tail even with ``maxLength`` on the string:
    most answers here land under 512 tokens and a few run past 1,024. A clean
    experiment needs a budget most answers overrun and twice that budget
    almost all of them fit, and those two conditions have no overlap on this
    distribution. Loosening the guard to admit the failures would be
    threshold-fitting, so the guard stays and the suite reports NOT MEASURED.

    What the contaminated runs agree on is the direction and rough size: 1.48x
    and 1.60x slower, in runs whose contamination pushes the estimate *down*
    in the first case and mixes retries with outright failures in the second.
    An over-run costs about half again as much wall clock, plus the answers
    that never arrive — which in a real gate is the gate failing.

    The average understates it, though, and the concurrency sweep showed how.
    A fan-out is ``asyncio.gather``, so it finishes when its *slowest* request
    does — and a retry starts after everything else has already finished. At
    width 16 the pass carrying one truncation took 41.06s against 19.08s for
    the pass carrying none: one request in sixteen more than doubled the
    batch. So the cost is not "a retry, amortised over N requests"; it is a
    retry appended to the end of the batch, and it grows with the width the
    fan-out is trying to buy.
    """
    _check_widths((width,), requests)
    budget = _TRUNCATING_TOKENS
    suite = Suite(
        id="truncation",
        question="What does an answer that overruns its budget cost?",
        note=(
            f"{requests} requests per pass at width {width}, one shared prefix. "
            f"The arms differ in one thing: the answer budget — "
            f"{config.max_tokens} tokens against {budget}, which the answers "
            f"overrun so llm_query retries them at {budget * 2}."
        ),
    )
    for cid, label, tokens in (
        ("fits", f"max_tokens = {config.max_tokens} (the answer fits)", None),
        ("truncates", f"max_tokens = {budget} (the answer is cut off)", budget),
    ):
        logger.info("truncation: {}", cid)
        suite.measurements.append(
            await measure(
                Condition(
                    id=cid,
                    label=label,
                    width=width,
                    payload=_shared,
                    max_tokens=tokens,
                ),
                config,
                requests,
                repeat,
            )
        )

    # The premise, checked. Either arm behaving unlike its name makes the gap
    # between them a measurement of something else, and a duration with the
    # wrong name on it is worse than no duration at all.
    fits, truncates = suite.measurements
    sent = requests * repeat
    if truncates.truncated < _TRUNCATING_FLOOR * sent:
        suite.note += (
            f"  NOT MEASURED: only {truncates.truncated} of {sent} requests in "
            f"the {budget}-token arm truncated, which moves a median by less "
            f"than the spread between passes. The answers are shorter than "
            f"this budget assumes — lower [_TRUNCATING_TOKENS] and run it "
            f"again."
        )
    elif fits.truncated:
        suite.note += (
            f"  NOT MEASURED: the {config.max_tokens}-token arm truncated "
            f"{fits.truncated} time(s), so both arms paid retries and the gap "
            f"understates the cost. Raise --max-tokens and run it again."
        )
    elif truncates.unanswered:
        # The third way the premise fails, and the one the counter alone
        # cannot show: `llm_query` doubles a truncated budget exactly once,
        # so an answer that overruns *twice* is given up on. This arm then
        # timed two failed attempts and no answer, which is not what a retry
        # costs — it is what a retry failing costs.
        suite.note += (
            f"  NOT MEASURED: {truncates.unanswered} request(s) in the "
            f"{budget}-token arm never came back — the doubled retry at "
            f"{budget * 2} truncated too, and llm_query doubles once. That "
            f"timed a failure, not a retry. Raise [_TRUNCATING_TOKENS]."
        )
    return suite
