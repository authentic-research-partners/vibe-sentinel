"""Timing one condition, honestly.

Everything here runs through :func:`vibe_sentinel.llm.llm_query` rather
than around it. A benchmark that built its own request body would be a
second copy of the thing it claims to measure, and the day the tool's
body gains a field the benchmark keeps reporting on the old one. So the
only thing a condition varies is what ``llm_query`` already takes as an
argument: how many of them run at once, what they are asked, and whether
they share a client.

Two consequences worth knowing. The retries in ``llm_query`` are inside
the measurement, because they are inside a real scan too — a pass that
had to retry is slower and should read as slower. And an answer that
comes back unparseable is counted rather than raised on, because the
number that matters is how many of the requests produced one: a fast
pass that answered nothing is not a fast pass, and this is where that
would otherwise hide.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from vibe_sentinel.backend import read_metrics
from vibe_sentinel.config import SentinelConfig
from vibe_sentinel.llm import llm_query
from vibe_sentinel.schemas import BackendMetrics, chars_to_words, every_field

#: Change lines in the synthetic drift report. Deliberately few, and the
#: reason is the whole shape of this file.
#:
#: A model rates what it is shown. Handed a 120-change report it wrote a
#: 20,903-character answer — every change, despite being asked for one —
#: because ``unbounded_schema`` strips the list's cap before the schema
#: goes on the wire, exactly as the tool intends in production. So answer
#: size tracks report size, and the tool's own drift pass handles that by
#: *scaling* the budget: ``analyze.answer_budget`` computes 5,367 tokens
#: for a 120-change report against a configured 2,048.
#:
#: A benchmark cannot use a budget that varies, because then the thing
#: being timed varies too. But the two needs it has are independent: the
#: prefix suite wants a *large shared prefix*, and the timing wants a
#: *small constant answer*. Only the change list drives the second. So
#: the bulk of the prefix is context the model reads and does not
#: enumerate, and the rateable part is one change.
#:
#: One, not three: measured. Three changes drew ~1,700 tokens of answer
#: and truncated half a pass at a 1,024-token budget, because the model
#: rates each change it is shown. One draws ~560 and lands inside it. The
#: question is narrow by design, so a longer list adds nothing it asks
#: about — it only adds answer the timing has to wait for.
REPORT_LINES = 1

#: Roughly how many characters of shared context ride in front of every
#: question — about 2,000 tokens, which is the range a real credentials
#: excerpt or safety history occupies, and enough that a prefix cache
#: hitting it is worth something measurable.
CONTEXT_CHARS = 6000

#: What a pass asks. Both halves are deliberately dull: the point is to
#: make the model spend a comparable amount of effort every time, not to
#: pose a question worth answering. The judged questions live in evals/.
#:
#: The narrowness is the measurement, not politeness to the model. An
#: earlier version said "for each change you have something to say about"
#: over a 120-line report, and a third of its requests hit ``max_tokens``
#: — which ``llm_query`` answers by retrying at double the budget. Every
#: truncated request therefore cost two generations, and the pass timed
#: the retry path rather than the fan-out. Asking about one directory
#: keeps the answer roughly the same size every time, which is what makes
#: a difference between two conditions attributable to the condition.
_SYSTEM = (
    "You rate changes in a codebase's organisation. Answer only about the "
    "one directory the question names: a one-sentence assessment and a "
    "single severity of low, medium or high. Do not discuss any other "
    "directory.\n\n"
)

#: Characters the ``note`` is allowed, and the one number behind both the
#: wire bound below and the sentence the model is told. Same discipline as
#: the package's own bounds: the cap enforced and the cap advertised are one
#: value, converted for the model by ``schemas.chars_to_words``.
_NOTE_CHARS = 200

#: The bound, said to the model, and the fields, named to the model — both
#: through the tool's own helpers rather than a third wording of the same
#: idea. Not decoration: it is the fix for the failure this harness hit from
#: the outside and the package had already recorded from the inside. A
#: constrained decoder accepts whitespace between any two tokens for as long
#: as there is room, so a model that will not start a field nobody mentioned
#: pads newlines until the ceiling — and more room is only more whitespace.
#: ``schemas.every_field`` carries the measurement: 0 of 3 answers at 512,
#: 1,024 and 2,048 tokens, against 3 of 3 once the fields were named, and
#: ``length`` at 15 s becoming ``stop`` at 8 s.
#:
#: So the benchmark asks the way the tool asks. A harness that invented its
#: own phrasing would be timing a prompt nothing in the product sends.
_BRIEF = (
    f'Keep "note" to one sentence, under {chars_to_words(_NOTE_CHARS)} words. '
    f"Anything past that is cut off, so say the deciding thing first. "
    f"{every_field('severity', 'note')}"
)

#: ``max_tokens`` for every request in every suite. Declared here rather
#: than taken from ``[llm] max_tokens`` because an answer whose length
#: varies with the reader's config is a second variable in every
#: comparison — and because the tool's own budgets differ per call site
#: (the drift pass computes one from the report's size, the credentials
#: gate fixes 512). Fixing it here is what makes two runs on two machines
#: comparable at all. Measured against the question above: a narrow
#: answer lands well inside this, and the suites verify it did.
BENCH_MAX_TOKENS = 1024

#: The answer's shape, and the one place this harness deliberately does
#: *not* mirror the tool.
#:
#: Production schemas go on the wire through ``unbounded_schema``, which
#: strips every ``maxLength`` and list cap — a stated decision, because a
#: single ``maxLength`` collapses some grammar compilers, and an over-run
#: is clipped afterwards instead. The cost is that answer length is left
#: entirely to the model, and an 8B model's variance is wide: asked the
#: same narrow question with one change in front of it, it wrote 560
#: tokens once and ran past 1,024 the next time, and each over-run cost a
#: retry at double the budget.
#:
#: A comparison cannot carry that as a hidden variable — two conditions
#: would differ by how talkative the model happened to be. So the bound
#: is on the wire here, which makes every answer the same small size and
#: truncation impossible by construction. It is a declared parameter of
#: the measurement, not a shortcut: a backend whose grammar compiler
#: cannot take ``maxLength`` fails loudly on the first request rather
#: than quietly measuring something else.
_BENCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
        "note": {"type": "string", "maxLength": _NOTE_CHARS},
    },
    "required": ["severity", "note"],
    "additionalProperties": False,
}


def _context(chars: int) -> str:
    """Filler the model reads and does not enumerate.

    Prose rather than a list, and that is the point: a list invites an
    entry per item in the answer, which is what made answer size track
    prefix size. This is context to be understood, so it costs prefill —
    which is what the prefix suite measures — and nothing in the answer.
    """
    sentence = (
        "This project keeps its modules thin and expects a directory to hold "
        "one concern; a count that climbs while the concern stays fixed is "
        "the shape worth noticing, and a count that climbs because a concern "
        "was split is not. "
    )
    return (sentence * (chars // len(sentence) + 1))[:chars]


def report(
    lines: int = REPORT_LINES, nonce: str = "", chars: int = CONTEXT_CHARS
) -> str:
    """A synthetic drift report: a large shared context, a short change list.

    ``nonce`` is what makes a prefix *not* shared. It goes at the front
    rather than the back, because a cache that matches on a prefix is
    defeated by a difference at the front and unbothered by one at the
    end — putting it last would report a cache hit as a cache miss.
    """
    head = f"{_SYSTEM}<CONTEXT id={nonce or 'shared'}>\n{_context(chars)}\n</CONTEXT>\n"
    body = "\n".join(
        f"dir:src/module_{i:03d}  modules  {3 + i % 4} -> {4 + i % 5}"
        for i in range(lines)
    )
    return f"{head}<REPORT>\n{body}\n</REPORT>\n"


def question(index: int) -> str:
    """The user half. Distinct per request, so nothing dedupes them.

    It names a directory the report actually carries, because a question
    about a change that is not there is answered with a shrug of a
    different length.
    """
    named = index % REPORT_LINES
    return (
        f"Question {index}: is directory src/module_{named:03d} growing in a "
        f"way that matters? Rate that one directory and say why in one "
        f"sentence.\n\n{_BRIEF}"
    )


class Pass(BaseModel):
    """One timed run of ``requests`` requests at one width.

    ``truncated`` is how many answers the server cut off at ``max_tokens``
    during this pass, read from its metrics endpoint before and after. It
    is ``None`` where the backend publishes no metrics — most of them do
    not — and that is not the same as zero: one means "none were cut off",
    the other "we cannot see whether any were". A pass with truncations in
    it timed ``llm_query``'s retry, not the fan-out, and the report says so
    rather than letting the number pass as clean.
    """

    seconds: float
    answered: int
    unanswered: int
    truncated: int | None = None
    #: Prefix-cache blocks hit and queried during this pass, from the same
    #: before-and-after reading as ``truncated``. This is the mechanism the
    #: prefix suite is actually about, and it is worth recording separately
    #: from the timing because it is a *binary* signal where the timing is a
    #: noisy one: whether the server reused the prefill does not depend on
    #: how busy the card was, so it answers "is the cache working" even on a
    #: run whose seconds cannot separate two arms.
    prefix_hits: int | None = None
    prefix_queries: int | None = None
    #: Requests the server evicted and had to recompute during this pass.
    #:
    #: The mechanism that puts a ceiling on the fan-out, and the reason a
    #: width sweep needs more than a stopwatch. Throughput flattens gently and
    #: noisily; preemption is a cliff, and it is what a KV cache running out
    #: actually looks like. Reading it means a width can be called too wide
    #: for a reason rather than because a median moved.
    preemptions: int | None = None
    latencies: list[float] = Field(default_factory=list)

    @property
    def prefix_hit_rate(self) -> float | None:
        """Share of this pass's prefill blocks the server already had."""
        if self.prefix_hits is None or self.prefix_queries is None:
            return None
        return self.prefix_hits / self.prefix_queries if self.prefix_queries else None

    @property
    def per_second(self) -> float:
        return len(self.latencies) / self.seconds if self.seconds else 0.0

    @property
    def median_latency(self) -> float:
        return statistics.median(self.latencies) if self.latencies else 0.0

    @property
    def slowest(self) -> float:
        return max(self.latencies) if self.latencies else 0.0


class Condition(BaseModel):
    """One thing to time, and what makes it different from its siblings.

    ``payload`` takes a request index and returns ``(system, user)``. It
    is a callable rather than a pair of strings because the prefix suite
    needs a *different* system message per request, and the concurrency
    suite needs the same one — one hook covers both.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    label: str
    width: int
    payload: Callable[[int], tuple[str, str]]
    shared_client: bool = False
    #: Overrides the run's answer budget for this condition alone. Only the
    #: truncation suite sets it, and it is the one thing a condition is
    #: allowed to change about the request besides the payload and the
    #: client — because an over-run is a property of the budget, and pricing
    #: one means holding everything else still while that moves.
    max_tokens: int | None = None


class Measurement(BaseModel):
    """A condition, repeated. The median pass is the reported one."""

    id: str
    label: str
    width: int
    requests: int
    passes: list[Pass] = Field(default_factory=list)

    @property
    def seconds(self) -> float:
        return statistics.median(p.seconds for p in self.passes)

    @property
    def fastest(self) -> float:
        return min(p.seconds for p in self.passes)

    @property
    def slowest_pass(self) -> float:
        return max(p.seconds for p in self.passes)

    @property
    def per_second(self) -> float:
        return self.requests / self.seconds if self.seconds else 0.0

    @property
    def spread(self) -> float | None:
        """How far apart the passes were, as a share of the fastest.

        Reported beside the median rather than left in the JSON, because a
        median of two hides the one thing a reader needs in order to know
        whether a gap between two rows means anything. This project's own
        recorded sweep had a row whose passes were 41.06s and 19.08s — the
        median said 30.07 and looked as solid as every other row.
        """
        if len(self.passes) < 2:
            return None
        fastest = min(p.seconds for p in self.passes)
        slowest = max(p.seconds for p in self.passes)
        return (slowest - fastest) / fastest if fastest else None

    @property
    def unanswered(self) -> int:
        return sum(p.unanswered for p in self.passes)

    @property
    def truncated(self) -> int | None:
        """Answers cut off across every pass, or None if never observable."""
        seen = [p.truncated for p in self.passes if p.truncated is not None]
        return sum(seen) if seen else None

    @property
    def preemptions(self) -> int | None:
        """Requests evicted and recomputed across every pass, or None."""
        seen = [p.preemptions for p in self.passes if p.preemptions is not None]
        return sum(seen) if seen else None

    @property
    def prefix_hit_rate(self) -> float | None:
        """Prefill blocks the server already had, over every pass.

        Pooled rather than averaged: the passes are different sizes only by
        accident, and a rate of rates weights a short pass like a long one.
        """
        hits = [p.prefix_hits for p in self.passes if p.prefix_hits is not None]
        queries = [
            p.prefix_queries for p in self.passes if p.prefix_queries is not None
        ]
        if not hits or not queries:
            return None
        total = sum(queries)
        return sum(hits) / total if total else None


class Suite(BaseModel):
    """A question, and the conditions that answer it."""

    id: str
    question: str
    note: str = ""
    measurements: list[Measurement] = Field(default_factory=list)


async def _one_pass(
    condition: Condition,
    config: SentinelConfig,
    requests: int,
    offset: int,
) -> Pass:
    """Fire ``requests`` requests at ``condition.width`` and time the lot.

    ``offset`` shifts the request indices so a warm-up never sends the
    exact bytes the measured pass will send. Only the user half moves —
    the system prefix is the condition's business, and shifting that
    would warm the wrong thing.
    """
    if condition.max_tokens is not None:
        config = config.model_copy(update={"max_tokens": condition.max_tokens})
    limit = asyncio.Semaphore(condition.width)
    client = (
        httpx.AsyncClient(timeout=httpx.Timeout(config.llm_timeout, connect=10.0))
        if condition.shared_client
        else None
    )
    before = await read_metrics(config)
    latencies: list[float] = []
    answered = 0

    async def one(index: int) -> None:
        nonlocal answered
        system, user = condition.payload(index)
        async with limit:
            started = time.perf_counter()
            raw = await llm_query(
                system,
                user,
                _BENCH_SCHEMA,
                f"bench-{condition.id}-{index}",
                config=config,
                client=client,
            )
            latencies.append(time.perf_counter() - started)
        if raw is not None:
            answered += 1

    started = time.perf_counter()
    try:
        await asyncio.gather(*(one(offset + i) for i in range(requests)))
    finally:
        if client is not None:
            await client.aclose()
    elapsed = time.perf_counter() - started
    after = await read_metrics(config)
    return Pass(
        seconds=elapsed,
        answered=answered,
        unanswered=requests - answered,
        truncated=_truncated_between(before, after),
        prefix_hits=_delta(before, after, "prefix_hits"),
        prefix_queries=_delta(before, after, "prefix_queries"),
        preemptions=_delta(before, after, "preemptions"),
        latencies=latencies,
    )


def _delta(
    before: BackendMetrics | None, after: BackendMetrics | None, field: str
) -> int | None:
    """One counter's movement across a pass, or None where it was not visible.

    Both readings have to carry it. Missing either yields ``None`` — "we
    cannot see" — never a zero, which would read as "nothing happened".
    """
    if before is None or after is None:
        return None
    start, end = getattr(before, field), getattr(after, field)
    if start is None or end is None:
        return None
    return int(end - start)


def _truncated_between(
    before: BackendMetrics | None, after: BackendMetrics | None
) -> int | None:
    """Answers cut off at ``max_tokens`` during a pass, or None if unseen.

    Both readings have to carry the counter for the difference to mean
    anything. Missing either one yields ``None`` — "we cannot see" — never
    a zero that would read as "none were cut off".
    """
    return _delta(before, after, "truncated")


async def measure(
    condition: Condition,
    config: SentinelConfig,
    requests: int,
    repeat: int,
) -> Measurement:
    """Warm the condition, discard that, then time it ``repeat`` times.

    The warm-up is the condition's own payload at the condition's own
    width, so what it warms is what the pass will use: the prefix this
    condition shares (or does not), the batch size the scheduler will
    see, and the client path. It is smaller than a full pass because it
    is buying a state, not a number — but never smaller than the width,
    or the widest condition would enter its measured pass with a
    scheduler that has never seen a batch that size.
    """
    warm = min(requests, max(2, condition.width))
    await _one_pass(condition, config, warm, offset=-1000)
    passes = [
        await _one_pass(condition, config, requests, offset=run * requests)
        for run in range(repeat)
    ]
    return Measurement(
        id=condition.id,
        label=condition.label,
        width=condition.width,
        requests=requests,
        passes=passes,
    )
