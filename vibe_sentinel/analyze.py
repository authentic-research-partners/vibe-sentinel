"""The second model pass: reading the changes.

The comparison in :mod:`vibe_sentinel.inventory` finds *what* changed.
This asks the local model *whether it matters* — a package growing from
three modules to nineteen and a package growing from three to four are
the same kind of change and very different events.

**What it is asked is declared, not shipped.** A ``[[lens]]`` is one
question about the report, in the project's own words — "is
``api/handlers/`` becoming the place anything unclaimed gets put?" — and
it is the same shape as a ``[[danger]]`` or a ``[[secret]]``, for the
same reason. A probe says what is measured; a lens says what to make of
it, and a shipped list of signals cannot know which of your directories
is meant to stay thin. The built-in ``organization`` lens is one of
these rather than a special case, so a project can override it by id or
switch it off.

**One question per request, not one request per report.** Each lens is
its own call. The report — structure, changes, horizons, fitted
timelines — is the system message and is byte-identical across the
fan-out, so a server that caches prefixes prefills it once and only the
question differs. Same lesson as the safety gate's: a small model asked
to weigh several things at once weighs the obvious one and skims the
rest.

**A lens sees the timelines.** A horizon comparison and a Theil–Sen fit
are facts the mechanism already produced, and judging facts the
mechanism produced is this module's whole job. They stay readings —
nothing here moves a baseline, is stored, or reaches an exit code — but
a slope nobody interprets is a number in a report, and what a lens
exists to ask is usually about a direction rather than about today.

The model may only rate and comment on changes that were already found.
It cannot add one, and it cannot delete one: the change list going into
the prompt is the change list coming out of the report, with severity and
a note attached. That boundary is what keeps the drift set reproducible
while still getting a judgment about significance.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict

from vibe_sentinel.config import SentinelConfig
from vibe_sentinel.json_schema import clip_to_bounds
from vibe_sentinel.llm import llm_query
from vibe_sentinel.schemas import (
    _ANALYSIS_SCHEMA,
    ASSESSMENT_CHARS,
    MAX_RATED,
    NOTE_CHARS,
    chars_to_words,
    Change,
    DriftAnalysis,
    DriftReport,
    LensResult,
    Snapshot,
)

#: Worst first. A second lens may raise a change's severity and never
#: lower it — see :func:`_apply`.
_SEVERITY_ORDER = ("info", "low", "medium", "high")

#: Characters per token, worst case. JSON punctuation and short keys
#: tokenize worse than prose, so three is the conservative figure to size
#: a budget with.
_CHARS_PER_TOKEN = 3
#: What one rating costs besides its note: the key, the probe id, the
#: severity, and the JSON around them.
_RATING_OVERHEAD_CHARS = 140


def answer_budget(changes: int, floor: int) -> int:
    """``max_tokens`` for one lens, from the bounds its answer must fit.

    Arithmetic rather than a guess, which is what the bounds in
    :mod:`vibe_sentinel.schemas` are for: a note is capped, an assessment
    is capped, the list is capped, so the widest answer a lens can
    legitimately give is computable. What it replaces was not computable
    at all — a lens answering about a 21-change report wanted 2225 tokens
    against a configured 2048 and came back as half an answer, and the
    scan recorded it as a lens that did not answer.

    ``floor`` is the configured ``max_tokens``, and it is a floor rather
    than a ceiling because a reasoning model spends tokens before the
    answer begins. Whoever raised it for that reason keeps what they set.
    """
    rated = min(max(changes, 1), MAX_RATED)
    chars = rated * (NOTE_CHARS + _RATING_OVERHEAD_CHARS) + ASSESSMENT_CHARS + 200
    return max(floor, -(-chars // _CHARS_PER_TOKEN))


class Lens(BaseModel):
    """One question to ask about a drift report, in the project's words.

    ``question`` is literally the prompt, the same way a ``[[danger]]``'s
    is: write it as the question you would ask a colleague who has just
    been handed the report. It is asked about the report as a whole —
    the changes, what the same measurements did over each horizon, and
    what the fitted series are doing — so a question about a direction
    ("has this been climbing for a month?") is as answerable as one about
    today.

    ``watch`` names the probes this lens is about, and is the cheap stage:
    a lens whose probes did not move, did not move over a horizon, and
    have no fitted trend is not asked at all. Empty means every probe,
    which is what the built-in lens wants and what a project's own
    house-style question usually wants too.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    question: str
    watch: tuple[str, ...] = ()
    """Probe ids. Empty means every probe."""


ANALYZE_SYSTEM_PROMPT = f"""\
You review changes in the structural organization of a codebase between
two points in time. You are not reviewing code quality or correctness.

Severity:
- "high"   — the organization has changed in a way someone should decide on.
- "medium" — worth a look; a trend that would matter if it continues.
- "low"    — real but minor.
- "info"   — noise; no action.

Be strict about "high". A tool that calls everything significant gets
muted, and then it catches nothing at all.

You will be asked ONE question about the report below. Rate only the
changes listed in it, and only the ones your question is about — a change
your question has nothing to say about is one you leave out, not one you
rate to have rated it. Do not invent changes.

Reference each change by BOTH its exact key and the probe that reported
it: two probes can measure the same directory, so the key on its own does
not always say which change you mean.

Be brief. One sentence per note, under {chars_to_words(NOTE_CHARS)} words,
and at most {MAX_RATED} ratings. In "assessment", two or three sentences
answering your question over the report as a whole, under
{chars_to_words(ASSESSMENT_CHARS)} words. Anything past those is cut off,
so a paragraph about one change costs you the rest of the answer.

Use the horizons and the fitted timelines — a direction sustained over a
month, or a value that has left its own trend, can matter more than
anything that moved since the baseline.

Return strict JSON.
"""

ORGANIZATION_QUESTION = """\
Is each of these changes a meaningful shift in how the codebase is
organized, or is it ordinary growth?

Signals that a change is meaningful:
- Code appearing in a directory that did not hold code before.
- A directory meant to stay small growing substantially.
- A module's internal coupling rising sharply — it has become a hub.
- Commentary rising well out of line with the rest of the codebase.
- A structural pattern showing up somewhere it never appeared before.

Signals that a change is ordinary:
- Small movements consistent with normal feature work.
- Growth spread evenly rather than concentrated in one place.
- A directory growing in proportion to what it already was.
"""

#: The shipped set: one lens, asking what this tool has always asked.
#: Declared the same way a project's own are, so ``[drift] disable`` or a
#: ``[[lens]]`` reusing the id works on it like anything else.
BUILTIN_LENSES: tuple[Lens, ...] = (
    Lens(
        id="organization",
        title="Structural organization",
        question=ORGANIZATION_QUESTION,
    ),
)


# ---------------------------------------------------------------------------
# The declared set
# ---------------------------------------------------------------------------


def load_lenses(root: Path | None = None) -> tuple[Lens, ...]:
    """The active lens set: built-ins, layered with the project's own.

    Same rules as probes, dangers and secrets, for the same reason:

      - a ``[[lens]]`` with a NEW id **adds** one,
      - one reusing a built-in id **overrides** that built-in,
      - ``[drift] use = ["id", ...]`` keeps **only** those,
      - ``[drift] disable = ["id", ...]`` removes one,
      - ``[drift] use_builtins = false`` starts from nothing.

    Raises ``ValueError`` on a malformed set. A lens that cannot be read
    is a question this project asked and did not get, so it fails the
    scan rather than quietly narrowing what the model was asked.
    """
    import tomllib

    from vibe_sentinel.paths import CONFIG_FILENAME

    merged: dict[str, Lens] = {lens.id: lens for lens in BUILTIN_LENSES}
    if root is None:
        return tuple(merged.values())

    path = root / CONFIG_FILENAME
    if not path.is_file():
        return tuple(merged.values())
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ValueError(f"{path} is not readable as TOML: {e}") from e

    raw_settings = data.get("drift")
    settings: dict[str, Any] = raw_settings if isinstance(raw_settings, dict) else {}
    if not settings.get("use_builtins", True):
        merged = {}

    for index, raw in enumerate(data.get("lens", []), start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: [[lens]] #{index} is not a table.")
        lens = _lens_from_toml(raw, path, index)
        merged[lens.id] = lens

    keep = settings.get("use")
    if keep is not None:
        unknown = [i for i in keep if i not in merged]
        if unknown:
            known = ", ".join(sorted(merged)) or "(none)"
            raise ValueError(
                f"{path}: [drift] use names lens(es) that do not exist: "
                f"{', '.join(unknown)}. Available: {known}."
            )
        merged = {k: v for k, v in merged.items() if k in keep}

    disable = settings.get("disable", [])
    unknown = [i for i in disable if i not in merged]
    if unknown:
        known = ", ".join(sorted(merged)) or "(none)"
        raise ValueError(
            f"{path}: [drift] disable names lens(es) that do not exist: "
            f"{', '.join(unknown)}. Available: {known}. A stale entry here "
            f"silently asks nothing, so it is an error, not a no-op."
        )
    for lens_id in disable:
        del merged[lens_id]

    return tuple(merged.values())


def _lens_from_toml(raw: dict[str, object], path: Path, index: int) -> Lens:
    """One ``[[lens]]`` table, validated with its remediation named."""
    where = f"{path}: [[lens]] #{index}"
    lens_id = str(raw.get("id", "")).strip()
    if not lens_id:
        raise ValueError(
            f"{where} has no id. Every lens needs one to be overridden or "
            f"disabled by name."
        )
    question = str(raw.get("question", "")).strip()
    if not question:
        raise ValueError(
            f"{where} ({lens_id}) has no question. The question is what the "
            f"model is actually asked — a lens without one costs a request "
            f"and says nothing."
        )
    watch = raw.get("watch", [])
    if not isinstance(watch, list) or not all(isinstance(w, str) for w in watch):
        raise ValueError(
            f"{where} ({lens_id}) has watch={watch!r}. It takes a list of "
            f'probe ids — watch = ["file-length"] — or nothing at all, '
            f"which watches every probe."
        )
    return Lens(
        id=lens_id,
        title=str(raw.get("title", "")).strip() or lens_id,
        question=question,
        watch=tuple(watch),
    )


def unknown_watches(lenses: tuple[Lens, ...], probe_ids: set[str]) -> list[str]:
    """``lens.probe`` pairs naming a probe that does not exist.

    Checked by the caller before a scan runs rather than during one: a
    lens watching a probe that was renamed is never asked, and finding
    that out from a report that stayed quiet is the failure the ``disable``
    check above exists to prevent. Reported, not raised, so the one place
    that knows how to abort a command is the one that does it.
    """
    return [
        f"{lens.id} watches {name!r}"
        for lens in lenses
        for name in lens.watch
        if name not in probe_ids
    ]


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------


def build_context(report: DriftReport, current: Snapshot, guidance: str = "") -> str:
    """The shared half of the prompt: the whole report, once.

    This is the **system** message and it is byte-identical for every lens
    asked about this run, so a server that caches prefixes — vLLM does —
    prefills the report once however many questions are asked of it. The
    divergent tail is one lens's question, from :func:`build_question`.
    """
    summaries = "\n".join(
        f"- {pid}: {r.summary}"
        for pid, r in sorted(current.probes.items())
        if r.summary
    )
    changes = "\n".join(
        f"- key={c.key!r} probe={c.probe_id} kind={c.kind} "
        f"before={c.before} after={c.after} :: {c.label}"
        for c in report.changes
    )

    blocks = [ANALYZE_SYSTEM_PROMPT]
    if guidance.strip():
        # The licence gate's convention: a project's standing note about
        # how it wants things read, in front of every question rather
        # than repeated in each one.
        blocks.append(f"<HOUSE_NOTES>\n{guidance.strip()}\n</HOUSE_NOTES>")
    blocks.append(
        f"<CURRENT_STRUCTURE>\n{summaries or '(no probe summaries)'}\n"
        f"</CURRENT_STRUCTURE>"
    )
    blocks.append(
        f"<CHANGES_SINCE baseline={report.baseline_at!r}>\n"
        f"{changes or '(nothing moved since the baseline)'}\n</CHANGES_SINCE>"
    )
    horizons = _horizon_block(report)
    if horizons:
        blocks.append(horizons)
    timelines = _timeline_block(report)
    if timelines:
        blocks.append(timelines)
    return "\n\n".join(blocks)


def _horizon_block(report: DriftReport) -> str:
    """The same measurements, against runs further back.

    A horizon that reached nothing says so. A comparison labelled ``1m``
    that found an empty history is not one that found the codebase
    unchanged, and a model told the second thing will say the second
    thing.
    """
    if not report.horizons:
        return ""
    lines: list[str] = []
    for horizon in report.horizons:
        if horizon.unavailable:
            lines.append(f"- {horizon.horizon}: not measured — {horizon.unavailable}")
            continue
        moved = [c for c in horizon.changes if c.severity != "info"]
        head = f"- {horizon.horizon} (against a run {horizon.age_days:.0f} days old)"
        if not moved:
            lines.append(f"{head}: no movement")
            continue
        lines.append(f"{head}: {len(moved)} change(s)")
        lines += [
            f"    key={c.key!r} probe={c.probe_id} kind={c.kind} "
            f"before={c.before} after={c.after}"
            for c in moved
        ]
    return "<OVER_LONGER_HORIZONS>\n" + "\n".join(lines) + "\n</OVER_LONGER_HORIZONS>"


def _timeline_block(report: DriftReport) -> str:
    """What the whole recorded series is doing, fitted.

    Only the fits worth quoting reach the report at all — a direction the
    Mann–Kendall test called, or a point that left its own trend — so this
    is short by construction rather than by truncation.
    """
    if not report.trends:
        return ""
    lines: list[str] = []
    for fit in report.trends:
        lines.append(
            f"- key={fit.key!r} probe={fit.probe_id} {fit.direction} over "
            f"{fit.runs} runs: {fit.first_value:g} -> {fit.last_value:g} "
            f"(slope {fit.slope:+.3g}/run, tau {fit.tau:+.2f}, "
            f"p {fit.p_value:.3g})"
        )
        for hit in fit.anomalies:
            z = "?" if hit.z is None else f"{hit.z:+.1f}"
            lines.append(
                f"    off its own trend this run: {hit.value:g} where the fit "
                f"expected {hit.expected:.4g} ({z} robust deviations)"
            )
    return "<TIMELINES>\n" + "\n".join(lines) + "\n</TIMELINES>"


def build_question(lens: Lens) -> str:
    """The divergent half: one lens's question, in the project's words."""
    return (
        f"Answer this one question about the report above, and nothing else.\n\n"
        f"({lens.id}) {lens.title}\n\n{lens.question}"
    )


def watched(lens: Lens, report: DriftReport) -> bool:
    """Whether anything this lens watches has anything to say this run.

    The cheap stage, and the reason three declared lenses do not mean
    three requests on every scan. A lens watching nothing in particular
    is always asked; one naming its probes is asked when they moved,
    moved over a horizon, or have a fitted trend.
    """
    if not lens.watch:
        return True
    watch = set(lens.watch)
    if any(c.probe_id in watch for c in report.changes):
        return True
    if any(
        c.probe_id in watch
        for horizon in report.horizons
        for c in horizon.changes
        if c.severity != "info"
    ):
        return True
    return any(fit.probe_id in watch for fit in report.trends)


# ---------------------------------------------------------------------------
# The pass itself
# ---------------------------------------------------------------------------


async def analyze_drift(
    report: DriftReport,
    current: Snapshot,
    config: SentinelConfig | None = None,
    lenses: tuple[Lens, ...] | None = None,
    root: Path | None = None,
) -> DriftReport:
    """Ask each declared lens its question; attach what comes back.

    Returns the report unchanged when there is nothing to ask about or no
    lens answered. The mechanical severities from the comparison stay in
    place, so a run without a model still reports drift — just without
    the commentary.

    Every lens is accounted for in ``report.lenses``, asked or not. A
    question nobody asked must not read as a question that found nothing,
    which is the same rule an unavailable horizon keeps.
    """
    if report.first_run:
        return report
    if not (
        report.changes
        or report.trends
        or any(horizon.moved for horizon in report.horizons)
    ):
        return report

    config = config or SentinelConfig()
    active = lenses if lenses is not None else load_lenses(root)
    if not active:
        return report

    results: list[LensResult] = []
    asked = []
    for lens in active:
        if watched(lens, report):
            asked.append(lens)
        else:
            results.append(
                LensResult(
                    id=lens.id,
                    title=lens.title,
                    note=(
                        "not asked — nothing it watches moved, over any "
                        "horizon, and none of it has a fitted trend"
                    ),
                )
            )
    if not asked:
        report.lenses = results
        return report

    context = build_context(report, current, config.drift_guidance)
    tuned = config.model_copy(
        update={"max_tokens": answer_budget(len(report.changes), config.max_tokens)}
    )

    async def ask_all() -> list[Any]:
        # Free against a server that batches: the context above is one
        # prefix, prefilled once for the whole fan-out. Ollama serialises,
        # so set [drift] concurrency = 1 there and pay for it in latency.
        limit = asyncio.Semaphore(max(1, tuned.drift_concurrency))

        async def one(lens: Lens) -> tuple[Lens, dict[str, Any] | None]:
            async with limit:
                return lens, await llm_query(
                    context,
                    build_question(lens),
                    _ANALYSIS_SCHEMA,
                    f"analyze-drift-{lens.id}",
                    config=tuned,
                )

        return await asyncio.gather(
            *(one(lens) for lens in asked), return_exceptions=True
        )

    outcomes = await ask_all()

    by_pair = {(c.probe_id, c.key): c for c in report.changes}
    by_key: dict[str, list[Change]] = {}
    for entry in report.changes:
        by_key.setdefault(entry.key, []).append(entry)
    #: The severity each change has been given *by a lens*, so a later one
    #: can raise it and never lower it.
    best: dict[tuple[str, str], str] = {}

    for item in outcomes:
        if isinstance(item, BaseException) or not isinstance(item, tuple):
            logger.warning("drift analysis: a lens failed ({})", item)
            continue
        lens, raw = item
        if raw is None:
            results.append(
                LensResult(
                    id=lens.id,
                    title=lens.title,
                    asked=True,
                    note="asked, but the model did not answer",
                )
            )
            continue
        try:
            analysis = DriftAnalysis.model_validate(clip_to_bounds(DriftAnalysis, raw))
        except Exception as e:  # noqa: BLE001 - one bad answer must not lose the rest
            logger.warning("drift analysis: unusable answer from {} ({})", lens.id, e)
            results.append(
                LensResult(
                    id=lens.id,
                    title=lens.title,
                    asked=True,
                    note=f"asked, but the answer was unusable: {e}",
                )
            )
            continue
        rated = _apply(analysis, report, lens, by_pair, by_key, best)
        results.append(
            LensResult(
                id=lens.id,
                title=lens.title,
                asked=True,
                answered=True,
                assessment=analysis.assessment,
                rated=rated,
            )
        )

    order = {lens.id: i for i, lens in enumerate(active)}
    results.sort(key=lambda r: order.get(r.id, len(order)))
    report.lenses = results

    answered = [r for r in results if r.answered]
    if not answered:
        logger.warning(
            "drift analysis: no lens answered; keeping mechanical severities"
        )
        return report

    if len(answered) == 1:
        report.assessment = answered[0].assessment
    else:
        report.assessment = "\n\n".join(
            f"{r.title or r.id}: {r.assessment}" for r in answered if r.assessment
        )
    report.analyzed = True
    logger.info(
        "drift analysis: {}/{} lens(es) answered, {} change(s) rated",
        len(answered),
        len(asked),
        sum(r.rated for r in answered),
    )
    return report


def _apply(
    analysis: DriftAnalysis,
    report: DriftReport,
    lens: Lens,
    by_pair: dict[tuple[str, str], Change],
    by_key: dict[str, list[Change]],
    best: dict[tuple[str, str], str],
) -> int:
    """Attach one lens's ratings. Returns how many landed.

    A change is identified by (probe_id, key), the same pair the
    ``changes`` table stores and ``idx_changes_key_run`` indexes. Keying
    on the key alone collapsed two probes measuring one directory into a
    single entry, so one probe's rating landed on the other's change and
    the first silently kept its mechanical severity. Where the model
    names only a key that belongs to one change, that is unambiguous and
    enough; where it belongs to several, the rating is dropped rather
    than guessed. A mechanical severity is a worse answer than the right
    one and a much better answer than the wrong one.

    Across lenses the worst rating wins. Each lens is asked its own
    narrow question, so most of them have nothing to say about most
    changes — and a lens that was not looking for something must not talk
    down the lens that was.
    """
    rated = 0
    for item in analysis.changes:
        change = by_pair.get((item.probe_id, item.key))
        if change is None:
            sharing = by_key.get(item.key, [])
            if len(sharing) == 1:
                change = sharing[0]
            elif sharing:
                logger.warning(
                    "drift analysis ({}): {} probes report key {!r} and the "
                    "model named none of them — rating dropped, mechanical "
                    "severity kept",
                    lens.id,
                    len(sharing),
                    item.key,
                )
                continue
        if change is None:
            logger.debug(
                "drift analysis ({}): model referenced unknown key {}",
                lens.id,
                item.key,
            )
            continue
        pair = (change.probe_id, change.key)
        seen = best.get(pair)
        if seen is not None and _rank(item.severity) <= _rank(seen):
            continue
        best[pair] = item.severity
        change.severity = item.severity
        change.note = item.note
        rated += 1
    return rated


def _rank(severity: str) -> int:
    """Where a severity sits, worst highest. Unknown values sort lowest."""
    try:
        return _SEVERITY_ORDER.index(severity)
    except ValueError:
        return -1
