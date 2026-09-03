"""Rendering a scan and its drift for the three audiences.

- ``terminal``: a person reading the result of a run.
- ``json``: CI, a dashboard, another tool.
- ``agent``: the coding agent whose work moved the structure. Stated as
  a constraint rather than a remark, because the point of the tool is
  that structural drift comes back as something to answer for.

The command journal has its own renderers at the bottom of this module.
It reads oldest-first, unlike the run listings: it is a log of what
happened, and a log reads forwards.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

from vibe_sentinel.journal import (
    HOOK_EVENT_FIELDS,
    AgentSessionRecord,
    CommandRecord,
    ReviewRecord,
)
from vibe_sentinel.schemas import (
    Anomaly,
    DriftReport,
    GateState,
    HorizonDrift,
    Snapshot,
    TrendFit,
)
from vibe_sentinel.trends import ANOMALY_MIN_RUNS, ANOMALY_Z, SIGNIFICANCE

if TYPE_CHECKING:  # pragma: no cover - annotations only, never imported
    from vibe_sentinel.credentials import Findings, Policy, Secret
    from vibe_sentinel.db.maintenance import DatabaseSize, HealthReport, PruneResult

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}
_KIND_MARK = {"appeared": "+", "disappeared": "-", "grew": "^", "shrank": "v"}


def _ordered(report: DriftReport):
    return sorted(
        report.changes,
        key=lambda c: (_SEVERITY_RANK.get(c.severity, 9), c.probe_id, c.key),
    )


def render_terminal(snapshot: Snapshot, report: DriftReport) -> None:
    """Print the structure and any drift."""
    print("Structure")
    for probe_id in sorted(snapshot.probes):
        result = snapshot.probes[probe_id]
        if not result.ok:
            print(f"  {probe_id}: FAILED — {result.error}")
            continue
        print(
            f"  {probe_id}: {result.summary or f'{len(result.observations)} observation(s)'}"
        )
        if result.filled:
            params = ", ".join(f"{k}={v}" for k, v in sorted(result.filled.items()))
            print(f"    parameters: {params}")

    if report.first_run:
        print(f"\n{report.assessment}")
        return

    if not report.changes:
        print(f"\nNo change since {report.baseline_at}.")
        # Not a return. "Nothing since the baseline" is exactly the run
        # where a horizon has something to say — the baseline may be two
        # days old, and the thing that has been growing for a month is
        # invisible to it by construction. A lens reads those, so it can
        # have an answer on a run where nothing moved.
        if report.assessment:
            print(f"\n{report.assessment}")
        _render_horizons(report)
        _render_trends(report)
        _render_lenses(report)
        return

    # Said before the severities rather than after them, because it is what
    # they mean. `analyzed` is False under --no-model and whenever the model
    # did not answer, and a `[high]` that nobody rated is the mechanical one
    # compare() assigned. Same rule as render_agent's and as the credentials
    # report's "NOT adjudicated" line: never claim a review that did not
    # happen, in the renderer a person actually reads.
    rated = (
        "reviewed by an independent local model"
        if report.analyzed
        else "mechanical comparison only — the model did not review these"
    )
    print(f"\nDrift since {report.baseline_at} ({rated})")
    for c in _ordered(report):
        mark = _KIND_MARK.get(c.kind, "?")
        print(f"  {mark} [{c.severity}] {c.describe()}")
        if c.note:
            print(f"      {c.note}")

    if report.assessment:
        print(f"\n{report.assessment}")

    significant = sum(1 for c in report.changes if c.severity != "info")
    print(
        f"\nvibe-sentinel: {len(report.changes)} change(s), "
        f"{significant} worth attention."
    )
    _render_horizons(report)
    _render_trends(report)
    _render_lenses(report)


def _render_lenses(report: DriftReport) -> None:
    """Which questions were asked of this report, and which got an answer.

    Silent for a single lens that answered: the assessment above is its
    answer, and the accounting adds nothing to it. It speaks up the moment
    a lens was declared and not asked, or asked and unanswered — that is
    the case a reader would otherwise take for "it looked and found
    nothing", which is the same rule an unavailable horizon keeps.
    """
    if not report.lenses:
        return
    if len(report.lenses) == 1 and report.lenses[0].answered:
        return
    answered = sum(1 for r in report.lenses if r.answered)
    print(f"\nLenses  ({answered} of {len(report.lenses)} answered)")
    for result in report.lenses:
        if result.answered:
            rated = (
                f"{result.rated} change(s) rated"
                if result.rated
                else "answered; rated nothing"
            )
            print(f"  {result.id:<22} {rated}")
        else:
            print(f"  {result.id:<22} {result.note}")


#: Horizon changes shown per horizon before the rest are counted. A month
#: of a busy repository is a wall, and a wall is read as a wall — the
#: whole list is in `--format json`, which is where a tool reads it.
_HORIZON_SHOWN = 5


def _horizon_changes(horizon: HorizonDrift) -> list:
    return sorted(
        (c for c in horizon.changes if c.severity != "info"),
        key=lambda c: (_SEVERITY_RANK.get(c.severity, 9), c.probe_id, c.key),
    )


def _render_horizons(report: DriftReport) -> None:
    """Print what the same measurements did over each declared horizon.

    Below the drift and clearly apart from it. These are measured from a
    different point in time, no model rated them, and none of them
    reaches the exit code — say all three, because a section that looks
    like the drift section will be read as the drift section.
    """
    if not report.horizons:
        return
    print(
        "\nAlso moved, over longer horizons  (mechanical comparison; "
        "no model rated\nthese and none of them changes the exit code)"
    )
    for horizon in report.horizons:
        if horizon.unavailable:
            print(f"  {horizon.horizon:<4} not measured — {horizon.unavailable}")
            continue
        where = f"run {horizon.run_id}, {horizon.age_days:.0f}d ago"
        changes = _horizon_changes(horizon)
        if not changes:
            print(f"  {horizon.horizon:<4} {where} — no movement")
            continue
        print(f"  {horizon.horizon:<4} {where} — {len(changes)} change(s)")
        for c in changes[:_HORIZON_SHOWN]:
            mark = _KIND_MARK.get(c.kind, "?")
            print(f"         {mark} [{c.severity}] {c.describe()}")
        if len(changes) > _HORIZON_SHOWN:
            print(f"         … and {len(changes) - _HORIZON_SHOWN} more")


def _p(value: float) -> str:
    """A p-value as a reader should see it.

    ``p<0.001`` rather than ``p=0.000``, which is not a probability and
    reads as one. Three decimals is already more than the number
    deserves: it comes from a normal approximation to a rank statistic,
    and its fourth digit is an artefact of that approximation.
    """
    return "p<0.001" if value < 0.001 else f"p={value:.3f}"


#: Fits shown in a scan before the rest are counted. A tree with three
#: hundred observations has more steady slopes than anybody reads, and
#: `vibe-sentinel trend` is where the whole list lives.
_TREND_SHOWN = 5


def _trend_line(fit: TrendFit) -> str:
    """The numbers, in the order somebody reads them.

    The fitted change before the endpoints, because the endpoints carry
    whatever noise they happen to carry and the fit is the claim. Tau
    beside p because a direction can be certain and negligible at once,
    and only tau says which.
    """
    return (
        f"{fit.slope:+.4g} per run, {fit.first_value:g} -> {fit.last_value:g} "
        f"over {fit.runs} run(s)   tau={fit.tau:+.2f}  {_p(fit.p_value)}"
    )


def _anomaly_line(fit: TrendFit, hit: Anomaly) -> str:
    """One departure, in the terms the series can actually support.

    A z-score where there is a spread to measure one in, and the plain
    fact where there is not — a series that held at one value every run
    has no deviations, and printing ``z=+0.0`` for the moment it moved
    would be the one reading that makes it look like nothing."""
    if hit.z is None:
        return (
            f"{hit.value:g} this run, against {hit.expected:g} on every "
            f"one of the last {fit.runs} runs"
        )
    return (
        f"{hit.value:g} this run where the trend expected "
        f"{hit.expected:.4g}   z={hit.z:+.1f}"
    )


def _render_trends(report: DriftReport) -> None:
    """Print what the recorded series are doing, and what left one today.

    Third section, and apart from the other two for the same reason they
    are apart from each other: this is a fit over the whole history, not
    a comparison of two points, nobody rated it, and it changes no exit
    code.
    """
    if not report.trends:
        return
    fitted = max(f.runs for f in report.trends)
    print(
        f"\nSustained trends  (Theil–Sen slope and Mann–Kendall p over "
        f"{fitted} run(s);\nno model rated these and none of them changes "
        "the exit code)"
    )
    for fit in report.trends[:_TREND_SHOWN]:
        for hit in fit.anomalies:
            print(f"  ! anomaly  {fit.probe_id}  {fit.key}")
            print(f"               {_anomaly_line(fit, hit)}")
        if fit.significant:
            mark = "^" if fit.direction == "rising" else "v"
            print(f"  {mark} {fit.direction:<8} {fit.probe_id}  {fit.key}")
            print(f"               {_trend_line(fit)}")
    if len(report.trends) > _TREND_SHOWN:
        print(
            f"         … and {len(report.trends) - _TREND_SHOWN} more — "
            "see `vibe-sentinel trend`"
        )


def render_trend_report(fits: list[TrendFit], runs: int, min_runs: int) -> None:
    """The `trend` command: every fit, and every anomaly inside a series.

    Unlike the scan's section this lists the anomalies *within* the
    window, not only the newest. Both are true; the difference is that
    somebody typed this and is looking at the history on purpose, where
    a scan repeating a jump from run 12 on every run for fifty runs is
    the thing nobody can act on.
    """
    moving = [f for f in fits if f.significant]
    unsettled = [f for f in fits if f.anomalies]

    print(
        f"Fitted {len(fits)} observation(s) with at least {min_runs} run(s) "
        f"each, over the\nlast {runs} run(s) — Theil–Sen slope, Mann–Kendall "
        "significance, and anomalies\nagainst each series' own trend. No "
        "model is involved in any of it."
    )
    print(f"\nTrends ({len(moving)} of {len(fits)} significant at p<={SIGNIFICANCE:g})")
    if not moving:
        print("  none — every fitted series is flat within the test's power")
    for fit in moving:
        mark = "^" if fit.direction == "rising" else "v"
        print(f"  {mark} {fit.direction:<8} {fit.probe_id}  {fit.key}")
        print(f"      {_trend_line(fit)}")
        print(
            f"      the fit accounts for {fit.fitted_change:+.4g} of "
            f"{fit.last_value - fit.first_value:+.4g}"
        )

    print(f"\nAnomalies ({len(unsettled)} series with a point off its trend)")
    if not unsettled:
        print(
            f"  none — every point sits within {ANOMALY_Z:g} robust deviations "
            f"of its fit,\n  which is the threshold {ANOMALY_MIN_RUNS} runs of "
            "history can carry without\n  calling one clean value in six a "
            "surprise"
        )
    for fit in unsettled:
        print(f"  {fit.probe_id}  {fit.key}")
        for hit in fit.anomalies:
            where = "this run" if hit.run_id is None else f"run {hit.run_id}"
            print(f"      {where:<10} {hit.at[:10]}  {_anomaly_line(fit, hit)}")


def render_json(
    snapshot: Snapshot, report: DriftReport, gates: GateState | None = None
) -> None:
    """Write structure, drift and gate state as one JSON object to stdout.

    ``gates`` is always present as a key, empty when the gates did not
    run. A consumer that has to tell "no findings" from "not checked"
    should not have to infer it from a missing field.
    """
    json.dump(
        {
            "snapshot": snapshot.model_dump(),
            "drift": report.model_dump(),
            "gates": (gates or GateState()).model_dump(),
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")


def render_agent(report: DriftReport, gates: GateState | None = None) -> str:
    """Return the drift, and what stands, as a block addressed to the agent.

    The gate findings are appended rather than merged. An agent told
    "nothing drifted" while a live key sits in the tree has been told the
    truth about the wrong question, and the two answers are only
    trustworthy while they stay distinguishable.
    """
    # Appended to every return below, not only the last. A baseline run
    # and a run with no drift are exactly the runs where saying nothing
    # about a key in the tree would be worst — the first because a
    # standing finding is *in* that baseline, the second because "nothing
    # changed" is the sentence an agent stops reading after.
    standing = (
        _agent_gate_block(gates)
        + _agent_horizon_block(report)
        + _agent_trend_block(report)
    )

    if report.first_run:
        return (
            "VIBE SENTINEL: BASELINE RECORDED\n"
            "The structure of this codebase has been recorded. Later runs "
            "will report organizational drift against it." + standing
        )

    significant = [c for c in _ordered(report) if c.severity != "info"]
    if not significant:
        # The assessment survives here: a lens reads the horizons and the
        # fitted series, so "nothing drifted against the baseline" is the
        # verdict it is most likely to have something to add to.
        read = f"\n\n{report.assessment}" if report.assessment else ""
        return (
            "VIBE SENTINEL: NO STRUCTURAL DRIFT\n"
            f"The organization of this codebase is unchanged since "
            f"{report.baseline_at}.{read}" + standing
        )

    reviewed = (
        " and reviewed by an independent local model"
        if report.analyzed
        else " (mechanical comparison only — the model did not review these)"
    )
    lines = [
        "VIBE SENTINEL: STRUCTURAL DRIFT DETECTED",
        "",
        (
            f"{len(significant)} change(s) to how this codebase is organized, "
            f"measured against the structure recorded at {report.baseline_at}"
            f"{reviewed}. Account for each one before this work is accepted."
        ),
        "",
    ]
    for i, c in enumerate(significant, start=1):
        lines.append(f"{i}. [{c.severity.upper()}] {c.describe()}")
        lines.append(f"   probe: {c.probe_id}")
        if c.note:
            lines.append(f"   why:   {c.note}")
        lines.append("")

    if report.assessment:
        lines.append(f"Overall: {report.assessment}")
        lines.append("")

    lines.append(
        "For each change: either restore the previous organization, or "
        "state why the new structure is the intended one. Do not silently "
        "re-baseline — accepting drift is a deliberate act, done with "
        "`vibe-sentinel scan --update`."
    )
    return "\n".join(lines) + standing


def _agent_horizon_block(report: DriftReport) -> str:
    """What moved over a longer horizon, as its own block after the drift.

    Empty unless a horizon actually moved. A horizon that is quiet says
    nothing the drift verdict above it has not already said, and a horizon
    with no run to reach back to is a fact about this history rather than
    about the code the agent just wrote — both belong in the terminal
    report, which a person reads, and neither is a constraint.

    Stated as context, not as a failure, because it is not one: these did
    not fail the scan and re-baselining would not clear them, since a
    horizon reaches back to a fixed point regardless of where the baseline
    sits.
    """
    moved = [w for w in report.horizons if _horizon_changes(w)]
    if not moved:
        return ""
    lines = ["", "", "VIBE SENTINEL: MOVEMENT OVER A LONGER WINDOW", ""]
    lines.append(
        "Not drift against the baseline and not a failure — this is what the same\n"
        "measurements did over a longer arc, which is where structure moves too\n"
        "slowly to trip any single comparison. Nobody rated these."
    )
    lines.append("")
    for horizon in moved:
        changes = _horizon_changes(horizon)
        lines.append(
            f"Over {horizon.horizon} (run {horizon.run_id}, "
            f"{horizon.age_days:.0f}d ago) — {len(changes)} change(s):"
        )
        for c in changes[:_HORIZON_SHOWN]:
            lines.append(f"  [{c.severity.upper()}] {c.describe()}  ({c.probe_id})")
        if len(changes) > _HORIZON_SHOWN:
            lines.append(f"  … and {len(changes) - _HORIZON_SHOWN} more")
        lines.append("")
    lines.append(
        "If a direction here is the intended one, say so. If it is not, this is "
        "the\npoint to correct it — nothing above will fail while it continues."
    )
    return "\n".join(lines).rstrip()


def _agent_trend_block(report: DriftReport) -> str:
    """A direction, or a value that just left one, as its own block.

    Only what this scan can act on: a series whose fit the test stands
    behind, and a value measured today that departed from it. Empty
    otherwise, on the same rule as the horizon block — an agent told
    about forty steady slopes reads none of them.
    """
    moving = [f for f in report.trends if f.significant or f.anomalies]
    if not moving:
        return ""
    lines = ["", "", "VIBE SENTINEL: WHERE THE HISTORY IS HEADING", ""]
    lines.append(
        "Not drift against the baseline and not a failure. This is the recorded\n"
        "history fitted — a direction is a property of every run, so no single\n"
        "comparison can show one. Nobody rated these."
    )
    lines.append("")
    for fit in moving[:_TREND_SHOWN]:
        for hit in fit.anomalies:
            lines.append(f"OFF TREND: {fit.key} is {_anomaly_line(fit, hit)}.")
            lines.append(f"  probe: {fit.probe_id}")
        if fit.significant:
            lines.append(
                f"{fit.direction.upper()}: {fit.key} {fit.slope:+.4g} per run, "
                f"{fit.first_value:g} -> {fit.last_value:g} over {fit.runs} "
                f"runs ({_p(fit.p_value)})."
            )
            lines.append(f"  probe: {fit.probe_id}")
        lines.append("")
    lines.append(
        "A direction you intended is worth saying so once. One you did not is "
        "worth\nstopping now: nothing above will fail while it continues, and "
        "it will not\nreverse on its own."
    )
    return "\n".join(lines).rstrip()


def _agent_gate_block(gates: GateState | None) -> str:
    """The standing findings, as their own block after the drift.

    Empty when nothing stands, so the drift block is unchanged for a tree
    the gates are happy with.
    """
    if gates is None:
        return ""
    broken = gates.broken
    failing = gates.failing
    unconfigured = gates.unconfigured
    if not failing and not broken and not unconfigured:
        return ""
    lines = ["", "", "VIBE SENTINEL: FINDINGS THAT STAND", ""]
    if failing:
        lines.append(
            f"{len(failing)} thing(s) are true of this tree right now. These are "
            "not drift and\nre-baselining does not clear them — only removing the "
            "cause, or a pin recording\nthe decision, does."
        )
        lines.append("")
        for i, finding in enumerate(sorted(failing, key=lambda f: (f.gate, f.key)), 1):
            rated = "" if finding.adjudicated else " (not adjudicated)"
            lines.append(f"{i}. [{finding.gate.upper()}] {finding.label}{rated}")
            if finding.risk:
                lines.append(f"   git:   {finding.risk}")
            if finding.reason:
                lines.append(f"   fix:   {_one_line(finding.reason, 88)}")
            lines.append("")
    for report in broken:
        lines.append(
            f"The {report.gate} gate did not complete: {report.error} "
            f"Nothing below it was checked."
        )
        lines.append("")
    for report in unconfigured:
        lines.append(
            f"The {report.gate} gate has no policy in this project, so it did "
            f"not run. Do not report it as passing."
        )
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# What the gates say is true right now
# ---------------------------------------------------------------------------

#: Failing first, then pinned decisions, then everything else. Not a
#: severity ordering — these are states, and the only ranking that means
#: anything is "does this still need somebody".
_GATE_ORDER = {True: 0, False: 1}


def render_gate_state(state: GateState) -> None:
    """Print what the gates found to be true of the tree, this run.

    Printed on every scan, whether or not anything moved, because that is
    the difference between this section and the drift above it. A licence
    or a credential does not stop being true because it was also true
    last week, and the run where nobody looked at it is exactly the run
    where saying nothing would be wrong.
    """
    if not state.reports:
        return
    print("\nState")
    for report in state.reports:
        if not report.configured:
            # Not a failure and not a pass. Naming the missing block is
            # the whole content of this line.
            print(f"  {report.gate}: no policy declared — this gate did not run")
            print(f"      {_one_line(report.error, 92)}")
            continue
        if not report.ok:
            # Never a clean line. A gate that could not run has not
            # reported that the tree is fine.
            print(f"  {report.gate}: DID NOT COMPLETE — {report.error}")
            continue
        mark = f"{len(report.failing)} failing" if report.failing else "clean"
        print(f"  {report.gate}: {mark} — {report.summary}")

    failing = state.failing
    if not failing:
        return
    print()
    for finding in sorted(failing, key=lambda f: (f.gate, f.key)):
        where = f"  ({finding.risk})" if finding.risk else ""
        rated = "" if finding.adjudicated else "  [not adjudicated]"
        print(f"  [{finding.gate}] {finding.label}{where}{rated}")
        if finding.detail:
            for line in finding.detail.splitlines()[:6]:
                print(f"      | {line[:100]}")
        if finding.reason:
            print(f"      -> {_one_line(finding.reason, 92)}")
    print(
        f"\n{len(failing)} finding(s) stand. These are states, not drift: each "
        f"one is reported\nagain on every scan until the cause is removed or a "
        f"pin records the decision.\nA baseline does not settle them — see "
        f"`vibe-sentinel licenses`, `packages`, `credentials`."
    )


# ---------------------------------------------------------------------------
# The agent command journal
# ---------------------------------------------------------------------------

#: Commands are shown on one line. A heredoc is stored whole and read
#: back whole; it is only the listing that flattens it.
_LINE_WIDTH = 96


def _one_line(text: str, width: int = _LINE_WIDTH) -> str:
    """Collapse a possibly multi-line command to a single readable line."""
    flat = " ".join(text.split())
    if len(flat) <= width:
        return flat
    return flat[: width - 1] + "…"


def _short(session_id: str, width: int = 8) -> str:
    return session_id[:width] if session_id else "?"


def render_commands(commands: list[CommandRecord], show_session: bool = True) -> None:
    """Print the command log oldest-first, the way a log is read."""
    if not commands:
        print("No commands recorded for that filter.")
        return

    print(f"{'when':<19}  {'session':<8}  {'actor':<16}  {'tool':<10}  command")
    for c in reversed(commands):
        session = _short(c.session_id) if show_session else ""
        print(
            f"{c.occurred_at[:19]:<19}  {session:<8}  {c.actor[:16]:<16}  "
            f"{c.tool_name[:10]:<10}  {_one_line(c.describe())}"
        )
    print(f"\n{len(commands)} command(s), oldest first.")


def render_actors(actors: list[AgentSessionRecord]) -> None:
    """Print who has been running commands.

    One row per actor, not per session: the main thread and each subagent
    under it are listed separately, which is the whole point of keeping
    ``agent_id`` in the first place.
    """
    if not actors:
        print("No sessions recorded yet.")
        return

    print(f"{'session':<36}  {'actor':<20}  {'cmds':>5}  last seen")
    for a in actors:
        print(
            f"{a.session_id[:36]:<36}  {a.actor[:20]:<20}  "
            f"{a.command_count:>5}  {a.last_seen_at[:19]}"
        )
    total = sum(a.command_count for a in actors)
    print(f"\n{len(actors)} actor(s), {total} command(s).")


def render_tool_counts(counts: list[tuple[str, int]]) -> None:
    """Print how often each tool was called."""
    if not counts:
        print("No commands recorded yet.")
        return
    width = max(len(name) for name, _ in counts)
    for name, n in counts:
        print(f"  {name:<{width}}  {n:>6}")


def render_commands_json(commands: list[CommandRecord]) -> None:
    """Write the command log as one JSON array to stdout."""
    json.dump([c.as_dict() for c in commands], sys.stdout, indent=2)
    sys.stdout.write("\n")


def render_observed_fields(scanned: int, fields: list[tuple[str, int]]) -> None:
    """Print which payload fields actually arrive, against the expected set.

    The journal reads Claude Code's hook payload, which is another
    program's output and can change under it. This says what has really
    been sent, so "the main thread has an empty ``agent_id``" is a
    statement about recorded data rather than about documentation.
    """
    if scanned == 0:
        print("No commands recorded yet — nothing to check the payload against.")
        return

    declared = set(HOOK_EVENT_FIELDS) - {"tool_input"}
    seen = dict(fields)
    print(f"Payload fields carrying a value, over the last {scanned} event(s):\n")
    for name in sorted(declared | set(seen)):
        count = seen.get(name, 0)
        mark = " " if name in declared else "+"
        print(f"  {mark} {name:<22} {count:>7}  {100 * count / scanned:5.1f}%")

    missing = sorted(n for n in declared if seen.get(n, 0) == 0)
    undeclared = sorted(n for n in seen if n not in declared)
    if missing:
        print(f"\nExpected but never populated: {', '.join(missing)}")
        print(
            "  Normal for agent_id / agent_type until a subagent has run, and "
            "for prompt_id\n  on calls made before the first user prompt. Not "
            "normal for the rest."
        )
    if undeclared:
        print(
            f"\n+ Sent but not declared by this build: {', '.join(undeclared)}\n"
            "  Kept in envelope_json, unparsed. Claude Code's payload has "
            "grown a field."
        )


_VERDICT_MARK = {"unsafe": "!", "unclear": "?", "safe": ".", "unreviewed": "-"}


def render_reviews(rows: list[ReviewRecord]) -> None:
    """Print recorded safety verdicts, newest first.

    ``unreviewed`` rows are the honest ones: triage flagged the command,
    the model never answered, and the command ran. They are listed rather
    than hidden, because a gate that quietly stopped working looks
    exactly like a gate with nothing to report.
    """
    if not rows:
        print("No commands have been reviewed.")
        return

    print(f"{'when':<19}  {'v':<1} {'verdict':<10} {'actor':<14} command")
    for r in rows:
        mark = _VERDICT_MARK.get(r.verdict, "?")
        blocked = " [BLOCKED]" if r.enforced else ""
        # A `safety --check` verdict is about a command somebody typed to
        # tune the gate, not one an agent ran. It shares this table, so
        # the listing has to say which — the same honesty rule `reviewed`
        # keeps about whether the model answered.
        tag = " [check]" if r.mode == "check" else ""
        print(
            f"{r.reviewed_at[:19]:<19}  {mark} {r.verdict:<10} {r.actor[:14]:<14} "
            f"{_one_line(r.command or r.tool_name, 44)}{blocked}{tag}"
        )
        if r.reason:
            print(f"{'':<21}   {_one_line(r.reason, 88)}")

    asked = sum(1 for r in rows if r.reviewed)
    declared = sum(1 for r in rows if not r.reviewed and r.verdict != "unreviewed")
    lost = sum(1 for r in rows if r.verdict == "unreviewed")
    refused = sum(1 for r in rows if r.enforced)
    checks = sum(1 for r in rows if r.mode == "check")
    parts = [f"{len(rows)} flagged", f"{asked} put to the model"]
    if declared:
        # Decided by the danger set rather than failed — the opposite of
        # the line below, and worth not confusing with it.
        parts.append(f"{declared} settled without asking")
    if checks:
        parts.append(f"{checks} typed at `safety --check`, not run by an agent")
    parts.append(f"{refused} blocked")
    print("\n" + ", ".join(parts) + ".")
    if lost:
        print(
            f"  {lost} row(s) marked 'unreviewed' were flagged and ran anyway — "
            "the model did not answer.\n  Check it with: vibe-sentinel backend "
            "status"
        )


def render_triage(command: str, signals: tuple[str, ...]) -> None:
    """Print what the pattern stage makes of one command."""
    if not signals:
        print(f"  not flagged: {_one_line(command)}")
        print("  This never reaches the model.")
        return
    print(f"  flagged: {_one_line(command)}")
    print(f"  signals: {', '.join(signals)}")


def render_dangers(dangers: object) -> None:
    """Print the active danger set: what is checked for, and what is asked."""
    items = list(dangers)  # type: ignore[call-overload]
    if not items:
        print("No dangers are active — nothing will ever be escalated.")
        print("  [safety] use_builtins = false and no [[danger]] declared.")
        return
    checks = [d for d in items if d.escalates]
    context = [d for d in items if not d.escalates]

    for danger in checks:
        scope = "" if danger.applies_to == "command" else f"  ({danger.applies_to})"
        print(f"\n{danger.id}{scope}")
        print(f"  {danger.title}")
        if danger.pattern:
            print(f"  matches:  {danger.pattern}")
        if danger.verdict:
            print(f"  verdict:  {danger.verdict}  (declared; the model is not asked)")
        else:
            print(f"  asks:     {danger.question}")

    if context:
        print("\nContext signals — never escalate on their own, attached to a")
        print("verdict once something else has:")
        for danger in context:
            print(f"  {danger.id:<22} {danger.title}")

    print(f"\n{len(checks)} check(s), {len(context)} context signal(s).")


# ---------------------------------------------------------------------------
# The database's own upkeep
# ---------------------------------------------------------------------------

_FINDING_MARK = {"critical": "!!", "warning": " !", "notice": "  "}


def _bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def render_db_status(
    size: DatabaseSize, upkeep: list[tuple[str, str, bool, int, str]]
) -> None:
    """Print what the database weighs, and when it was last looked at."""
    print(f"History database: {size.path}")
    print(f"  file           {_bytes(size.file_bytes):>12}")
    if size.wal_bytes:
        print(f"  write-ahead log{_bytes(size.wal_bytes):>12}")
    print(
        f"  free pages     {_bytes(size.free_bytes):>12}"
        f"   {size.freelist_count} of {size.page_count} pages"
    )

    print(f"\n{'table':<18}{'rows':>10}{'data':>11}{'indexes':>11}")
    for table in size.tables:
        if size.detailed:
            print(
                f"{table.name:<18}{table.rows:>10,}"
                f"{_bytes(table.table_bytes):>11}{_bytes(table.index_bytes):>11}"
            )
        else:
            print(f"{table.name:<18}{table.rows:>10,}{'-':>11}{'-':>11}")
    if not size.detailed:
        print("\n  (this SQLite build has no dbstat — row counts only)")

    if upkeep:
        print("\nRecent upkeep")
        for at, kind, ok, findings, detail in upkeep:
            note = detail or (f"{findings} finding(s)" if findings else "clean")
            mark = "" if ok else "  FAILED"
            print(f"  {at}  {kind:<8} {note}{mark}")
    else:
        print("\nNo upkeep recorded yet — the first check runs on the next command.")


def render_db_health(report: HealthReport) -> None:
    """Print a health check: what was measured, and what needs attention."""
    size = report.size
    print(f"Checked {size.path} at {report.at}")
    print(
        f"  {_bytes(size.total_bytes)}, schema "
        f"v{report.schema_current} of v{report.schema_target}"
    )
    integrity = report.integrity_ok
    if integrity is None:
        print("  integrity: not checked")
    else:
        print(f"  integrity: {'ok' if integrity else 'FAILED'}")

    findings = report.findings
    if not findings:
        print("\nNothing needs attention.")
        return

    print("")
    for finding in sorted(
        findings, key=lambda f: {"critical": 0, "warning": 1}.get(f.severity, 2)
    ):
        print(f"{_FINDING_MARK.get(finding.severity, '  ')} {finding.message}")
        if finding.remediation:
            print(f"     {finding.remediation}")


def render_db_findings(report: HealthReport) -> str:
    """One line per finding, for the automatic check's warning."""
    return "; ".join(f.message for f in report.attention)


def render_prune(result: PruneResult) -> None:
    """Print what a prune removed, or what it would remove."""
    verb = "Deleted" if result.applied else "Would delete"
    print(f"Records older than {result.cutoff}")
    print(f"  scope: {', '.join(result.scope)}")
    for table, count in result.deleted.items():
        if count:
            print(f"  {verb.lower():<12} {count:>9,}  {table}")
    if not result.total:
        print("  nothing that old is recorded.")
        return
    if result.kept_runs:
        print(
            f"  keeping the newest {len(result.kept_runs)} run(s) and the "
            f"baseline whatever their age"
        )
    if result.applied:
        print(f"\nBacked up first: {result.backup_path}")
        print("  That backup is the revert — copy it back over the database.")
        if result.freed_bytes:
            print(f"  File shrank by {_bytes(result.freed_bytes)}.")
        else:
            print(
                "  The file has not shrunk: the pages are free but still "
                "allocated. Reclaim them with `vibe-sentinel db vacuum`."
            )
    else:
        print("\nDry run — nothing deleted. Add --apply to do it.")


def render_pattern_match(
    pattern: str,
    scanned: int,
    matched: list[tuple[str, bool]],
    limit: int = 12,
) -> None:
    """What a candidate pattern would have caught, against real history.

    Writing a triage pattern is otherwise done blind: `'/mnt/live'` reads
    like a path and means "this substring anywhere", and you find out
    from a stray match on a commit message weeks later — or never find
    out about the `cd /mnt/live && rm -rf .` it missed, which is the half
    that matters. The journal already holds what the agents really ran,
    so the pattern can be measured instead of guessed at.
    """
    if scanned == 0:
        print("No commands recorded yet, so there is nothing to try it against.")
        print("  Install the hook first: vibe-sentinel hook --install")
        return

    already = sum(1 for _, flagged in matched if flagged)
    print(f"{pattern!r} against {scanned} recorded command(s):\n")
    if not matched:
        print("  matches nothing.")
        print(
            "  That is worth a second look — a pattern that never fires is "
            "indistinguishable\n  from one you forgot to write."
        )
        return

    for command, flagged in matched[:limit]:
        mark = "already flagged" if flagged else "NEW"
        print(f"  {mark:<15} {_one_line(command, 72)}")
    if len(matched) > limit:
        print(f"  … and {len(matched) - limit} more")

    share = 100 * len(matched) / scanned
    print(
        f"\n{len(matched)} of {scanned} ({share:.1f}%) would escalate; "
        f"{already} of those the current set already catches, "
        f"{len(matched) - already} are new."
    )
    if share > 20:
        print(
            "  Over a fifth of everything the agent runs is a lot to send to a "
            "model.\n  Tighten it, or accept the latency deliberately."
        )


# ---------------------------------------------------------------------------
# Credentials at rest
# ---------------------------------------------------------------------------

#: Worst first. `unclear` outranks `placeholder` because it is the one that
#: needs a person, and `unreviewed` sits at the bottom on its own: it is not
#: a verdict about the file, it is the absence of one.
_SECRET_ORDER = ("real", "unclear", "placeholder", "pinned", "unreviewed")

_EXPOSURE_NOTE = {
    "tracked": "tracked by git",
    "untracked": "untracked — one `git add -A` from being committed",
    "ignored": "ignored by git",
    "outside": "outside the repository",
    "unknown": "git status unknown",
}


def render_credentials(findings: Findings, policy: Policy) -> None:
    """Print what was found, what was decided, and who decided it."""
    from vibe_sentinel.credentials import KEYCHAIN_ADVICE

    scan = findings.scan
    print(f"policy: {policy.source}")
    print(
        f"{scan.files_read} file(s) read, {scan.files_skipped} skipped, "
        f"{len(scan.candidates)} candidate(s) flagged"
    )
    if findings.reviewed:
        print(f"adjudicated by {findings.model}")
    elif findings.judgements:
        # Never let a listing read as a review. Same rule as DriftReport.analyzed.
        print("NOT adjudicated — every verdict below is mechanical")
    if findings.note:
        print(f"\n{findings.note}")
    if scan.git_note:
        print(f"note: {scan.git_note}")
    if scan.truncated:
        print(
            "the walk stopped early: this is a floor, not an inventory. "
            "Raise [credentials] max_files."
        )
    for problem in scan.unreadable[:10]:
        print(f"  unread: {problem}")
    print()

    shown = sorted(
        findings.judgements,
        key=lambda j: (
            _SECRET_ORDER.index(j.verdict) if j.verdict in _SECRET_ORDER else 9,
            j.candidate.path,
            j.candidate.rule,
        ),
    )
    for judgement in shown:
        candidate = judgement.candidate
        if judgement.verdict in ("placeholder", "pinned"):
            continue
        where = _EXPOSURE_NOTE.get(candidate.exposure, candidate.exposure)
        print(f"  [{judgement.verdict}] {candidate.path}  ({where})")
        print(f"      {candidate.rule} — {candidate.title}")
        if judgement.reason:
            print(f"      {_one_line(judgement.reason, 92)}")
        for line in candidate.excerpt.splitlines()[:8]:
            print(f"      | {line[:100]}")
        print()

    counted = {v: 0 for v in _SECRET_ORDER}
    for judgement in findings.judgements:
        counted[judgement.verdict] = counted.get(judgement.verdict, 0) + 1
    print(
        ", ".join(
            f"{counted.get(v, 0)} {v}" for v in _SECRET_ORDER if counted.get(v, 0)
        )
        or "nothing flagged"
    )

    ignored = findings.gitignored()
    if ignored:
        setting = policy.gitignored
        verb = {
            "warn": "reported, and NOT failing this gate",
            "deny": "failing this gate like any other",
            "allow": "dropped before anything was asked",
        }[setting]
        print(
            f"\n{len(ignored)} finding(s) sit in files git ignores — {verb} "
            f'([credentials] gitignored = "{setting}").'
        )

    failing = findings.failing(policy)
    if not failing:
        print("\nNothing here needs a decision.")
        return

    print(
        f"\n{len(failing)} finding(s) need a decision. For each: move the value "
        "out of the file, or\nrecord why it is acceptable — a pin is scoped to "
        "the rules it names, so accepting\none for a path does not accept the "
        "next one there.\n"
        "\n  [[credentials.pin]]\n"
        '  paths  = ["tests/fixtures/*.pem"]\n'
        '  accept = ["private-key-file"]\n'
        '  reason = """generated for the TLS tests; never deployed"""\n'
        '  verified = "<today>"\n'
    )
    print(KEYCHAIN_ADVICE)


def render_secret_rules(secrets: tuple[Secret, ...]) -> None:
    """Print the active rule set: what is looked for, and what is asked."""
    if not secrets:
        print("No credential rules are active — nothing will ever be flagged.")
        print("  [credentials] use_builtins = false and no [[secret]] declared.")
        return
    for kind, heading in (
        ("path", "Files whose purpose is holding credentials"),
        ("content", "Credentials hardcoded into files that should hold none"),
    ):
        group = [s for s in secrets if s.applies_to == kind]
        if not group:
            continue
        print(f"\n=== {heading} ===")
        for secret in group:
            print(f"\n{secret.id}")
            print(f"  {secret.title}")
            print(f"  matches:  {secret.pattern}")
            if secret.verdict:
                print(
                    f"  verdict:  {secret.verdict}  (declared; the model is not asked)"
                )
            else:
                print(f"  asks:     {_one_line(secret.question, 88)}")
    print(f"\n{len(secrets)} rule(s) active.")
