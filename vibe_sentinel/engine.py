"""The scan: run, compare, analyze, record — and the gates beside it.

    for each probe:
        run the command, with the parameters its config declares
        collect observations
    snapshot  ──compare──▶  baseline run from the history database
              └─compare──▶  newest run at least 1w old   ┐  declared
              └─compare──▶  newest run at least 1m old   ┘  horizons
                                  │
                          model rates the changes
                                  │
                      persist run + parameters + observations + changes
                                  │
                              DriftReport

Probes run sequentially. They are subprocesses doing filesystem work, not
GPU calls, and there are a handful of them — a scheduler here would add
failure modes without buying wall-clock.

Measuring is now entirely mechanical: the model is asked to rate what
changed and, in the credentials gate, to judge a candidate — never to
decide what to look at. That is the whole of its job here.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

from loguru import logger

from vibe_sentinel.analyze import Lens, analyze_drift, load_lenses
from vibe_sentinel.config import SentinelConfig, load_config
from vibe_sentinel.db import get_db
from vibe_sentinel.db import gate_store, store as db_store
from vibe_sentinel.gates import collect_all
from vibe_sentinel import trends
from vibe_sentinel.horizons import age_days, horizon_cutoff
from vibe_sentinel.inventory import compare
from vibe_sentinel.exceptions import LLMConnectionError
from vibe_sentinel.packs import LoadedPack
from vibe_sentinel.paths import CONFIG_FILENAME
from vibe_sentinel.results import capture_provenance, capture_evidence
from vibe_sentinel.runner import run_probe
from vibe_sentinel.schemas import (
    DriftReport,
    GateState,
    HorizonDrift,
    ProbeResult,
    ScanContext,
    Snapshot,
    TrendFit,
)
from vibe_sentinel.templates import Probe


# The inner collector owns the selected target's timeout and process cleanup.
# Give its scanner process bounded room to import, encode and report that result.
_DEPENDENCY_READER_OVERHEAD_S = 30.0


def scan(
    probes: list[Probe],
    root: Path,
    config: SentinelConfig | None = None,
    *,
    context: ScanContext | None = None,
) -> Snapshot:
    """Run every probe and return the resulting structural snapshot.

    Synchronous, because measuring reaches nothing but the filesystem.
    Each probe runs with the values its ``[[probe.placeholders]]`` tables
    declare. The model rates what this finds; it never chooses what is
    measured, which is why nothing here reaches for one.
    """
    filled = [dict(p.defaults()) for p in probes]

    snapshot = Snapshot(
        root=root.as_posix(),
        model=(config.llm_model if config else ""),
        context=context,
    )
    for probe, values in zip(probes, filled, strict=True):
        started = time.monotonic()
        if (
            probe.source == "built-in"
            and probe.id == "dependency-versions"
            and probe.command[:3]
            == [sys.executable, "-m", "vibe_sentinel.probes.dependencies"]
        ):
            try:
                selected = config
                if selected is None:
                    candidate = root / CONFIG_FILENAME
                    selected = (
                        load_config(candidate)
                        if candidate.is_file()
                        else SentinelConfig()
                    )
                # Forward the selected settings, including programmatic configs.
                # The outer timeout must allow the inner owner to stop its target.
                bounded = probe.model_copy(
                    update={
                        "timeout_s": max(
                            probe.timeout_s,
                            selected.environment_timeout
                            + _DEPENDENCY_READER_OVERHEAD_S,
                        )
                    }
                )
                extra_args = [
                    f"--python={selected.environment_python}",
                    f"--environment-timeout={selected.environment_timeout}",
                    f"--environment-root={root.absolute()}",
                ]
                if selected.config_path is not None:
                    extra_args.append(f"--config={selected.config_path.absolute()}")
                result = run_probe(bounded, values, cwd=root, extra_args=extra_args)
            except (OSError, ValueError) as exc:
                result = ProbeResult(
                    probe_id=probe.id,
                    filled=values,
                    ok=False,
                    error=f"Dependency reader configuration failed: {exc}",
                )
        else:
            result = run_probe(probe, values, cwd=root)
        result.title = probe.title
        result.duration_ms = int((time.monotonic() - started) * 1000)
        snapshot.probes[probe.id] = result
    return snapshot


def drift_over_horizons(
    conn: sqlite3.Connection,
    snapshot: Snapshot,
    probes: list[Probe],
    horizons: list[str],
) -> list[HorizonDrift]:
    """Diff ``snapshot`` against the newest run at least each horizon old.

    The same ``compare`` against a different second end, which is the
    whole of it — a horizon changes where the comparison reaches back to
    and nothing else. Reads only; the result is not written anywhere,
    because both ends already are and this diff can be recomputed from
    them exactly. The measurements cannot: probes re-run against the code
    as it is now. The database keeps what cannot be regenerated.

    Runs before the current scan is recorded, so the selector can never
    pick the run being compared.
    """
    pack_only = bool(snapshot.probes) and all(
        pid.startswith("pack:") for pid in snapshot.probes
    )
    if pack_only:
        return [
            HorizonDrift(
                horizon=horizon,
                unavailable="Pack history coverage is baseline-only; horizons are not assessed.",
            )
            for horizon in horizons
        ]
    snapshot = snapshot.model_copy(
        update={
            "probes": {
                pid: result
                for pid, result in snapshot.probes.items()
                if not pid.startswith("pack:")
            }
        }
    )
    probes = [probe for probe in probes if not probe.id.startswith("pack:")]
    out: list[HorizonDrift] = []
    # Two horizons land on one run whenever the history is sparser than
    # the gap between them — `1w` and `2w` against a repository scanned
    # monthly are the same run, and that run's several hundred
    # observations should be read once.
    loaded: dict[int, Snapshot | None] = {}
    for horizon in horizons:
        record = db_store.run_at_or_before(conn, horizon_cutoff(horizon))
        previous = None
        if record is not None:
            if record.id not in loaded:
                loaded[record.id] = db_store.load_snapshot(conn, record.id)
            previous = loaded[record.id]
        if record is None or previous is None:
            oldest = db_store.earliest_run(conn)
            out.append(
                HorizonDrift(
                    horizon=horizon,
                    unavailable=(
                        f"no run recorded {horizon} or more ago; this "
                        f"history starts at {oldest.started_at}"
                        if oldest
                        else "no history to reach back into yet"
                    ),
                )
            )
            continue
        previous = previous.model_copy(
            update={
                "probes": {
                    pid: result
                    for pid, result in previous.probes.items()
                    if not pid.startswith("pack:")
                }
            }
        )
        mismatched = {
            probe.id
            for probe in probes
            if probe.compare_parameters
            and probe.id in snapshot.probes
            and (
                probe.id not in previous.probes
                or not previous.probes[probe.id].ok
                or previous.probes[probe.id].command
                != snapshot.probes[probe.id].command
                or previous.probes[probe.id].filled != snapshot.probes[probe.id].filled
            )
        }
        horizon_current = snapshot
        if mismatched:
            previous = previous.model_copy(
                update={
                    "probes": {
                        pid: result
                        for pid, result in previous.probes.items()
                        if pid not in mismatched
                    }
                }
            )
            horizon_current = snapshot.model_copy(
                update={
                    "probes": {
                        pid: result
                        for pid, result in snapshot.probes.items()
                        if pid not in mismatched
                    }
                }
            )
        out.append(
            HorizonDrift(
                horizon=horizon,
                unavailable="Parameter-guarded probe scopes are incomparable: "
                + ", ".join(sorted(mismatched))
                if mismatched
                else "",
                run_id=record.id,
                at=record.started_at,
                age_days=age_days(record.started_at),
                changes=compare(previous, horizon_current, probes).changes,
            )
        )
    return out


def fit_trends(
    conn: sqlite3.Connection,
    snapshot: Snapshot,
    limit_runs: int,
    min_runs: int = trends.MIN_RUNS,
    *,
    compare_parameters: frozenset[str] = frozenset(),
) -> list[TrendFit]:
    """Fit each recorded series, and score what this scan just measured.

    Out of sample, and that is the whole design. The series comes from
    the database; the value being judged is the one measured a moment
    ago and not in it. A point included in its own fit pulls the line
    towards itself and then reports how near it is to where it pulled
    it, which is how an anomaly detector comes to find nothing.

    Only the current value's departure is carried here, never the
    anomalies inside the series. Those are real, and `vibe-sentinel
    trend` lists them — but a jump at run 12 stays in a fifty-run window
    for fifty scans, and something reported on every scan with nothing
    that settles it is the shape this codebase has already been wrong
    about once. What the scan reports is what changed *for this scan*.

    Kept: a series with a direction the test will stand behind, or a
    value that departed from it today. Everything else is a fit of a
    number that is doing nothing, and there are hundreds of those.
    """
    recorded = db_store.series(conn, limit_runs=limit_runs, min_runs=min_runs)
    if not recorded:
        return []

    measured = {
        (probe_id, obs.key): obs
        for probe_id, result in snapshot.probes.items()
        if result.ok
        for obs in result.observations
    }

    out: list[TrendFit] = []
    scopes = {
        probe_id: db_store.matching_probe_runs(
            conn, probe_id, result.command, result.filled
        )
        for probe_id, result in snapshot.probes.items()
        if probe_id in compare_parameters
    }
    for (probe_id, key), points in recorded.items():
        if probe_id in scopes:
            points = [point for point in points if point.run_id in scopes[probe_id]]
        if probe_id.startswith("pack:"):
            continue
        observation = measured.get((probe_id, key))
        if observation is None or observation.value is None:
            continue
        fit = trends.fit_series(probe_id, key, points, min_runs=min_runs)
        if fit is None:
            continue
        today = trends.score(fit, observation.value, at=snapshot.generated_at)
        fit.anomalies = [today] if today else []
        if fit.significant or fit.anomalies:
            out.append(fit)

    # Anomalies first — a value that just left its trend is news, and a
    # slope that has been steady for forty runs is not. Then by how
    # one-way the movement is, which is the effect size; the p-value only
    # says the direction is real, never that it is large.
    out.sort(key=lambda f: (not f.anomalies, -abs(f.tau), f.probe_id, f.key))
    return out


async def scan_and_compare(
    probes: list[Probe],
    root: Path,
    config: SentinelConfig | None = None,
    update: bool = False,
    horizons: list[str] | None = None,
    trend_runs: int = 0,
    lenses: tuple[Lens, ...] | None = None,
    *,
    declared_packs: tuple[LoadedPack, ...] = (),
) -> tuple[Snapshot, DriftReport, int]:
    """Scan, diff against the baseline run, record, and return the verdict.

    Every scan is recorded — the history is the product, so a run that
    found drift is as worth keeping as one that did not. What ``update``
    controls is only which run later scans *compare against*: without it,
    a failing scan does not quietly become the new baseline.

    ``horizons`` names the points compared *as well as* the baseline, so
    that drift too slow to clear a tolerance between two scans still
    surfaces over a week or a month. They are a reading beside the verdict, never
    part of it: nothing about them moves the baseline, is stored, or
    reaches ``DriftReport.drifted``. The caller resolves them — the
    config's declared set, or ``--since`` — because which of those wins is
    a question about a command line, not about a scan.

    ``trend_runs`` is how far back the fits look, and 0 turns them off.
    They are the third reading beside the verdict and carry the same
    three properties as a horizon: no baseline moves, nothing is stored,
    no exit code changes.

    Returns ``(snapshot, report, run_id)``. A judge failure is recorded with the
    measured snapshot and ``"judgement" in snapshot.context.stage_errors``;
    callers must inspect the result status rather than assume a returned tuple
    means the judgement completed. Failed measurements/judgements never baseline.
    """
    selected = config
    if selected is None:
        candidate = root / CONFIG_FILENAME
        selected = load_config(candidate) if candidate.is_file() else SentinelConfig()
    lenses = lenses if lenses is not None else load_lenses(root)
    context = ScanContext(
        provenance=capture_provenance(
            root,
            selected,
            probes,
            lenses,
            horizons=horizons or [],
            trend_runs=trend_runs,
            declared_packs=declared_packs,
        )
    )
    snapshot = scan(probes, root, selected, context=context)
    if any(not result.ok for result in snapshot.probes.values()):
        context.stage_error = "measurement"

    with get_db(root) as conn:
        baseline_record = db_store.baseline_run(conn)
        baseline = (
            db_store.load_snapshot(conn, baseline_record.id)
            if baseline_record
            else None
        )
        context.baseline_run_id = baseline_record.id if baseline_record else None
        selected_ids = {probe.id for probe in probes}
        context.baseline_unmeasured = (
            tuple(
                probe_id
                for probe_id in sorted(selected_ids)
                if probe_id not in baseline.probes or not baseline.probes[probe_id].ok
            )
            if baseline is not None
            else ()
        )
        if baseline is not None:
            from vibe_sentinel.schemas import RunProvenance

            previous_packs = (
                baseline.context.provenance.packs
                if baseline.context is not None
                and isinstance(baseline.context.provenance, RunProvenance)
                else ()
            )
            previous_by_id = {pack.id: pack for pack in previous_packs}
            unavailable = set(context.baseline_unmeasured)
            for pack in (
                context.provenance.packs
                if isinstance(context.provenance, RunProvenance)
                else ()
            ):
                previous = previous_by_id.get(pack.id)
                if previous is None or (
                    previous.version,
                    previous.protocol,
                    previous.manifest_sha256,
                    previous.executables,
                ) != (
                    pack.version,
                    pack.protocol,
                    pack.manifest_sha256,
                    pack.executables,
                ):
                    unavailable.update(pack.selected_probes)
                else:
                    unavailable.update(
                        set(pack.selected_probes) - set(previous.selected_probes)
                    )
            for probe in probes:
                if not (probe._pack_id or probe.compare_parameters):
                    continue
                before = baseline.probes.get(probe.id)
                current = snapshot.probes.get(probe.id)
                if (
                    before is not None
                    and current is not None
                    and (
                        before.filled != current.filled
                        or before.command != current.command
                    )
                ):
                    unavailable.add(probe.id)
            context.baseline_unmeasured = tuple(sorted(unavailable))

        comparable_baseline = baseline
        if baseline is not None:
            incomparable_probes = {
                probe.id
                for probe in probes
                if (probe._pack_id or probe.compare_parameters)
                and probe.id in context.baseline_unmeasured
            }
            comparable_baseline = baseline.model_copy(
                update={
                    "probes": {
                        probe_id: result
                        for probe_id, result in baseline.probes.items()
                        if probe_id not in incomparable_probes
                    }
                }
            )
        report = compare(comparable_baseline, snapshot, probes)

        # Skipped on the first run, where there is nothing behind the
        # baseline to reach back to and every horizon would report the same
        # emptiness twice.
        if horizons and not report.first_run:
            report.horizons = drift_over_horizons(conn, snapshot, probes, horizons)

        # Same guard, and one more: a fit needs a series, so the early
        # runs of a project produce nothing here and say so rather than
        # quoting a direction three points cannot support.
        if trend_runs and not report.first_run:
            report.trends = fit_trends(
                conn,
                snapshot,
                trend_runs,
                compare_parameters=frozenset(
                    probe.id for probe in probes if probe.compare_parameters
                ),
            )

        # No `report.changes` guard: a lens reads the horizons and the
        # fitted series too, and the run where nothing moved since the
        # baseline is exactly the run where a month-long direction is the
        # only thing there is to say. analyze_drift decides whether there
        # is anything to ask about.
        try:
            report = await analyze_drift(
                report, snapshot, selected, lenses=lenses, root=root
            )
        except LLMConnectionError as exc:
            logger.error("Drift judgement failed: {}", exc)
            context.stage_errors += ("judgement",)
        if any(lens.asked and not lens.answered for lens in report.lenses):
            context.stage_errors += ("judgement",)

        complete = (
            not context.stage_error
            and not context.stage_errors
            and not capture_evidence(snapshot, report, GateState()).failure
        )
        if report.first_run and not complete:
            report.assessment = "No baseline recorded: this scan did not complete its measurements, judgement or evidence capture."

        run_id = db_store.save_run(
            conn,
            snapshot,
            report,
            baseline_run_id=baseline_record.id if baseline_record else None,
            # A completed first measurement establishes the initial baseline;
            # later acceptance takes --update. Failed work never becomes one.
            make_baseline=complete and (update or report.first_run),
            analyzed="judgement" not in (context.stage_error, *context.stage_errors),
        )

    logger.info("scan recorded as run {}", run_id)
    return snapshot, report, run_id


async def run_gates(
    root: Path,
    config: SentinelConfig | None = None,
    run_id: int | None = None,
) -> GateState:
    """Run the deterministic gates over ``root`` and record what they found.

    A sibling of :func:`scan_and_compare`, never a stage inside it. What
    these gates report is a state rather than a transition, so it must
    not pass through ``compare()``: a credential committed before the
    first scan is in the baseline, unchanged on every scan after it, and
    a diff would therefore never mention it again. Keeping the two apart
    is what makes "reported every run until a pin settles it" true.

    ``run_id`` ties the result to the scan that just recorded, so one
    query answers "what was this tree's state at run N". Running the
    gates alone leaves it ``None``.

    Async all the way down, like everything here that reaches the model:
    nothing below this opens an event loop of its own, so this composes
    with :func:`scan_and_compare` under the single ``asyncio.run`` the
    command makes. It did not always — ``adjudicate`` used to call
    ``asyncio.run`` itself, and the gate that broke was the only one
    whose answer costs a model call.

    The collect stage is mechanical and the adjudication is not, and
    both always run: the local model is part of this tool rather than an
    enhancement to it, so a gate that could not reach one fails instead
    of reporting what it managed without it.
    """
    state = await collect_all(root, config)
    with get_db(root) as conn:
        gate_store.save_gate_state(conn, state, root=root.as_posix(), run_id=run_id)
    logger.info(
        "gates recorded: {} finding(s), {} failing",
        sum(len(r.findings) for r in state.reports),
        len(state.failing),
    )
    return state
