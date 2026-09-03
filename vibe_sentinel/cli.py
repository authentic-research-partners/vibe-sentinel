"""Command handlers for ``vibe-sentinel``."""

from __future__ import annotations

import argparse
import json
import shlex
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:  # pragma: no cover - kept out of the hook's import cost
    import importlib.metadata as md

    from vibe_sentinel.licenses import Policy
    from vibe_sentinel.schemas import GateReport

from vibe_sentinel.config import SentinelConfig, load_config
from vibe_sentinel.db import SchemaMismatchError, db_path, get_db
from vibe_sentinel.db import journal_store
from vibe_sentinel.db import store as db_store
from vibe_sentinel.db.migration import (
    BACKUP_DIR_NAME,
    MigrationError,
    get_status,
    list_backups,
    run_migration,
)
from vibe_sentinel.report import (
    render_actors,
    render_db_health,
    render_db_status,
    render_prune,
    render_agent,
    render_commands,
    render_commands_json,
    render_credentials,
    render_dangers,
    render_gate_state,
    render_json,
    render_observed_fields,
    render_pattern_match,
    render_reviews,
    render_secret_rules,
    render_triage,
    render_terminal,
    render_tool_counts,
    render_trend_report,
)


def _load(args: argparse.Namespace) -> tuple[SentinelConfig, Path]:
    """Resolve the configuration and project root for one command.

    An explicit root takes its own config file if it has one. Without
    this, `vibe-sentinel safety /other/project` reads *this* directory's
    settings and reports them as that project's — which is how a
    configured timeout silently became the default.
    """
    from vibe_sentinel.paths import CONFIG_FILENAME

    config_arg = getattr(args, "config", None)
    root_arg = getattr(args, "root", None)

    if config_arg:
        config = load_config(Path(config_arg))
    elif root_arg:
        candidate = Path(root_arg) / CONFIG_FILENAME
        config = load_config(candidate if candidate.is_file() else None)
    else:
        config = load_config()

    root = Path(root_arg or config.project_dir or ".").resolve()
    return config, root


#: Commands that are themselves about the database's condition. The
#: automatic check is skipped for these — `db check` would run it twice,
#: and `migrate` is the remedy the check would be recommending.
_NO_AUTO_CHECK = frozenset({"db", "migrate", "backups", "hook"})


def auto_check(args: argparse.Namespace) -> None:
    """Run the periodic health check, at most once per configured interval.

    Called before every command that is not itself about the database.
    Findings go to stderr through the logger, never to stdout: a `scan
    --format json` piped into another tool must stay parseable no matter
    what this finds.

    Never raises, and never changes the command's exit code. A database
    that needs attention is worth saying so; it is not worth failing the
    scan somebody actually asked for.
    """
    if getattr(args, "command", None) in _NO_AUTO_CHECK:
        return
    try:
        from vibe_sentinel.db import maintenance

        config, root = _load(args)
        report = maintenance.maybe_check(root, config)
    except Exception as e:  # noqa: BLE001 - a check must not break a command
        logger.debug("automatic database check skipped: {}", e)
        return

    if report is None:
        return
    for finding in report.attention:
        logger.warning("database: {}", finding.message)
        if finding.remediation:
            logger.warning("database:   fix with: {}", finding.remediation)


def run_scan(args: argparse.Namespace) -> int:
    """Scan and report drift. 0 = no drift, 1 = drift, 2 = error.

    The scan pipeline is imported here rather than at module scope: it
    reaches the model client and httpx, and ``vibe-sentinel hook`` — which
    runs in front of every tool call the agent makes — must not pay for
    an import it never uses.
    """
    import asyncio

    from vibe_sentinel.analyze import load_lenses, unknown_watches
    from vibe_sentinel.engine import run_gates, scan_and_compare
    from vibe_sentinel.exceptions import LLMConnectionError
    from vibe_sentinel.horizons import parse_horizon
    from vibe_sentinel.schemas import DriftReport, GateState, Snapshot
    from vibe_sentinel.templates import (
        default_probes_path,
        load_probes,
        packaged_example_path,
        select_probes,
    )

    if getattr(args, "print_example", False):
        sys.stdout.write(packaged_example_path().read_text(encoding="utf-8"))
        return 0

    if getattr(args, "print_probes", False):
        sys.stdout.write(default_probes_path().read_text(encoding="utf-8"))
        return 0

    config, root = _load(args)
    config_arg = getattr(args, "config", None)
    try:
        pool = load_probes(
            config_paths=[Path(config_arg)] if config_arg else None,
            project_root=root,
        )
    except (FileNotFoundError, ValueError) as e:
        logger.error(str(e))
        return 2

    if getattr(args, "list_probes", False):
        # The values, not just the names. They are declared now, so this is
        # the whole of what the next scan will run — there is nothing left
        # for a model to decide and therefore nothing to preview separately.
        for p in pool:
            print(f"{p.id}  {p.title}")
            values = p.defaults()
            if values:
                shown = ", ".join(f"{k}={v}" for k, v in sorted(values.items()))
                print(f"    parameters:   {shown}")
            # shlex.join, not " ".join: a declared value can legitimately
            # hold a space or a semicolon — `docs=*.md; code=*.py` is one
            # argument — and printing it bare turns the preview into two
            # shell commands. The run is unaffected either way; the argv
            # never goes near a shell. This is about the line being
            # readable as the single command it describes.
            print(f"    command:      {shlex.join(p.fill(values))}")
        return 0

    ids_raw = getattr(args, "probes", None)
    try:
        probes = select_probes(
            [s.strip() for s in ids_raw.split(",") if s.strip()] if ids_raw else None,
            pool=pool,
        )
    except ValueError as e:
        logger.error(str(e))
        return 2

    if not probes:
        logger.error("vibe-sentinel: no probes to run.")
        return 2

    # Loaded here rather than inside the scan: a malformed [[lens]] is a
    # question this project asked and did not get, and finding that out
    # after every probe has run costs the measurements. Watches are checked
    # against the whole declared pool rather than this run's selection —
    # `--probes` narrows one run, it does not make a lens stale.
    try:
        lenses = load_lenses(root)
    except ValueError as e:
        logger.error(str(e))
        return 2
    stale = unknown_watches(lenses, {p.id for p in pool})
    if stale:
        logger.error(
            "vibe-sentinel: {} — no such probe. A lens watching a probe that "
            "does not exist is never asked, which reads as a lens that found "
            "nothing, so it is an error rather than a quiet report. Declared "
            "probes: {}",
            "; ".join(stale),
            ", ".join(sorted(p.id for p in pool)),
        )
        return 2

    # `--since` replaces the declared set rather than adding to it, and
    # the empty string is therefore how one run asks for no horizons at
    # all. `getattr(...) is None` distinguishes that from "not given",
    # which a truthiness test would fold together.
    since = getattr(args, "since", None)
    horizons = (
        [w.strip() for w in since.split(",") if w.strip()]
        if since is not None
        else list(config.drift_horizons)
    )
    try:
        for horizon in horizons:
            parse_horizon(horizon)
    except ValueError as e:
        logger.error(str(e))
        return 2

    fit_over = getattr(args, "fit", None)
    trend_runs = int(config.drift_trend_runs if fit_over is None else fit_over)
    if trend_runs < 0:
        logger.error("--fit takes a number of runs to look back over, or 0 for none.")
        return 2

    use_model = not getattr(args, "no_model", False)
    logger.info(
        "vibe-sentinel: scanning {} with {} probe(s) (model={})",
        root,
        len(probes),
        config.llm_model if use_model else "disabled",
    )

    async def everything() -> tuple[Snapshot, DriftReport, int, GateState]:
        """Both stages, one event loop.

        The gates are a sibling of the drift pipeline, never a step
        inside it — what they report is a state, and a state must not
        pass through ``compare()``, which reports a key when it appears
        and says nothing while it stays. But both reach the same model,
        so both belong under the same ``asyncio.run``: this is the only
        place in a scan that opens a loop.
        """
        snapshot, report, run_id = await scan_and_compare(
            probes,
            root,
            config=config,
            use_model=use_model,
            update=bool(getattr(args, "update", False)),
            horizons=horizons,
            trend_runs=trend_runs,
            lenses=lenses,
        )
        gates = await run_gates(root, config=config, use_model=use_model, run_id=run_id)
        return snapshot, report, run_id, gates

    try:
        snapshot, report, run_id, gates = asyncio.run(everything())
    except SchemaMismatchError as e:
        logger.error(str(e))
        return 2
    except LLMConnectionError as e:
        # The endpoint is down. That is a setup problem with a known fix,
        # not a stack trace — say so and name the way out.
        logger.error(str(e))
        return 2

    fmt = getattr(args, "format", None) or config.output_format
    if fmt == "json":
        render_json(snapshot, report, gates)
    elif fmt == "agent":
        print(render_agent(report, gates))
    else:
        render_terminal(snapshot, report)
        render_gate_state(gates)
        print(f"\nrecorded as run {run_id} in {db_path(root)}")

    failed = [p for p, r in snapshot.probes.items() if not r.ok]
    if failed:
        logger.error("probe(s) failed: {}", ", ".join(sorted(failed)))
        return 2
    broken = gates.broken
    if broken:
        logger.error("gate(s) did not complete: {}", ", ".join(r.gate for r in broken))
        return 2

    # Drift and standing findings both fail, and they fail for different
    # reasons: one says the shape moved, the other says something is true
    # of this tree that nobody has settled. A pin clears the second; a
    # new baseline never does.
    return 1 if report.drifted or gates.failing else 0


def run_history(args: argparse.Namespace) -> int:
    """List recorded runs, or show one run's detail."""
    _, root = _load(args)
    try:
        with get_db(root) as conn:
            run_id = getattr(args, "run", None)
            if run_id:
                snapshot = db_store.load_snapshot(conn, int(run_id))
                if snapshot is None:
                    logger.error("No run {} in {}", run_id, db_path(root))
                    return 2
                print(f"Run {run_id} — {snapshot.generated_at} — {snapshot.root}")
                model = snapshot.model or "(no model)"
                print(f"  model: {model}")
                for probe_id in sorted(snapshot.probes):
                    result = snapshot.probes[probe_id]
                    state = "ok" if result.ok else f"FAILED — {result.error}"
                    print(f"\n  {probe_id} [{state}] {result.duration_ms} ms")
                    print(f"    {result.summary or '(no summary)'}")
                    if result.filled:
                        params = ", ".join(
                            f"{k}={v}" for k, v in sorted(result.filled.items())
                        )
                        print(f"    parameters: {params}")
                    if getattr(args, "verbose", False):
                        for obs in result.observations:
                            value = "" if obs.value is None else f"  [{obs.value:g}]"
                            risk = f"  !{obs.risk}" if obs.risk else ""
                            print(f"      {obs.key}{value}{risk}")
                risks = db_store.risks_at(conn, int(run_id))
                if risks:
                    print(f"\n  provenance risks recorded ({len(risks)}):")
                    for obs in risks:
                        print(f"    [{obs.risk}] {obs.label or obs.key}")
                changes = db_store.load_changes(conn, int(run_id))
                if changes:
                    print(f"\n  changes recorded ({len(changes)}):")
                    for c in changes:
                        print(f"    [{c.severity}] {c.describe()}")
                return 0

            runs = db_store.list_runs(conn, limit=int(getattr(args, "limit", 20) or 20))
            if not runs:
                logger.error(
                    "No runs recorded in {}. Record one with: vibe-sentinel scan",
                    db_path(root),
                )
                return 2
            counts = db_store.risk_counts(conn, limit=len(runs))
            print(
                f"{'run':>5}  {'when':<20} {'probes':>6} {'obs':>6} {'changes':>8}"
                f"  {'risks':<24}"
            )
            for r in runs:
                mark = " *baseline" if r.is_baseline else ""
                model = r.model or "no-model"
                # Risks are per-run totals, not drift: a run that carries three
                # orphans carries them whether or not anything changed.
                tally = counts.get(r.id, {})
                risk_summary = (
                    ", ".join(f"{k} {n}" for k, n in sorted(tally.items())) or "-"
                )
                print(
                    f"{r.id:>5}  {r.started_at:<20} {r.probe_count:>6} "
                    f"{r.observation_count:>6} {r.change_count:>8}  "
                    f"{risk_summary:<24}  {model}{mark}"
                )
            print("\n* = the run later scans compare against")
    except SchemaMismatchError as e:
        logger.error(str(e))
        return 2
    return 0


def run_trend(args: argparse.Namespace) -> int:
    """Fit the recorded history: slopes, their significance, and anomalies.

    The read-only half of what a scan reports in passing. A scan fits the
    history and scores the value it just measured against it; this fits
    the same history and shows the whole of it, including the points that
    left their trend earlier — which a scan deliberately does not repeat.
    """
    from vibe_sentinel import trends

    config, root = _load(args)
    try:
        with get_db(root) as conn:
            key = getattr(args, "key", None)
            if key:
                probe_id = getattr(args, "probe", None)
                if not probe_id:
                    logger.error("--probe is required when a key is given.")
                    return 2
                points = db_store.trend(
                    conn, probe_id, key, limit=int(getattr(args, "limit", 30) or 30)
                )
                if not points:
                    logger.error("No history for {} / {}", probe_id, key)
                    return 2
                fit = trends.fit_series(probe_id, key, points)
                print(f"{probe_id}  {key}")
                for point in points:
                    value = "-" if point.value is None else f"{point.value:g}"
                    marker = ""
                    if fit is not None:
                        expected = fit.intercept + fit.slope * point.run_id
                        marker = f"  (fit {expected:.4g})"
                        if any(a.run_id == point.run_id for a in fit.anomalies):
                            marker += "  << off trend"
                    print(f"  run {point.run_id:>4}  {point.at}  {value:>10}{marker}")
                if fit is None:
                    print(
                        f"\nToo short to fit: a direction needs at least "
                        f"{trends.MIN_RUNS} runs."
                    )
                    return 0
                print()
                render_trend_report([fit], runs=fit.runs, min_runs=trends.MIN_RUNS)
                return 0

            runs = int(getattr(args, "runs", None) or config.drift_trend_runs or 50)
            min_runs = int(getattr(args, "min_runs", 0) or trends.MIN_RUNS)
            recorded = db_store.series(conn, limit_runs=runs, min_runs=min_runs)
            fits = [
                fit
                for (probe_id, obs_key), points in recorded.items()
                if (
                    fit := trends.fit_series(
                        probe_id, key=obs_key, points=points, min_runs=min_runs
                    )
                )
                is not None
            ]
            if not fits:
                # 2, not 0: the command could not answer, which is what
                # that code means here and what it has always meant for
                # this one. Refusing is the honest result — three points
                # make a line through anything — and the way out is more
                # scans, so say that rather than printing an empty table.
                logger.error(
                    "Not enough history to fit: a direction needs at least "
                    "{} runs of the same observation. Run `vibe-sentinel "
                    "scan` a few more times, or lower the floor with "
                    "--min-runs.",
                    min_runs,
                )
                return 2
            fits.sort(key=lambda f: (not f.anomalies, -abs(f.tau), f.probe_id, f.key))
            render_trend_report(fits, runs=runs, min_runs=min_runs)
    except SchemaMismatchError as e:
        logger.error(str(e))
        return 2
    return 0


def run_migrate(args: argparse.Namespace) -> int:
    """Migrate the history database to this build's schema version."""
    _, root = _load(args)
    status = get_status(root)

    if not status.exists:
        logger.error(
            "No history database at {}. `vibe-sentinel scan` creates one at "
            "the current version.",
            status.db_path,
        )
        return 2

    print(f"History database: {status.db_path}")
    print(f"  current version: v{status.current_version}")
    print(f"  target version:  v{status.target_version}")

    if status.up_to_date:
        print("  up to date — nothing to do.")
        return 0

    print(f"  pending:         {', '.join(f'v{v}' for v in status.pending)}")

    if getattr(args, "dry_run", False):
        print(
            "\nDry run — nothing applied. The real run copies the database, "
            "migrates and verifies the copy, then swaps it in and keeps the "
            "original as a backup."
        )
        return 0

    try:
        result = run_migration(root)
    except MigrationError as e:
        logger.error(str(e))
        return 2

    if not result.success:
        logger.error("Migration failed: {}", result.error)
        logger.error("The original database was not modified.")
        return 2

    print(f"\nMigrated v{result.from_version} -> v{result.to_version}.")
    if result.backup_path:
        print(f"Previous database kept at: {result.backup_path}")
        print("To revert, copy that file back over the database.")
    return 0


def run_backups(args: argparse.Namespace) -> int:
    """List the backups migrations have left behind."""
    _, root = _load(args)
    backups = list_backups(db_path(root).parent / BACKUP_DIR_NAME)
    if not backups:
        print("No backups yet. One is taken automatically before a migration")
        print("or a prune; take one now with `vibe-sentinel db backup`.")
        return 0
    for b in backups:
        label = f"pre-v{b.pre_version}" if b.pre_version else b.kind
        size_kb = b.size_bytes / 1024
        print(f"{b.created_at}  {label:>10}  {size_kb:>9.1f} KB  {b.path.name}")
    print(f"\nIn: {db_path(root).parent / BACKUP_DIR_NAME}")
    print("To revert, copy a backup back over the database file.")
    return 0


def run_parameters(args: argparse.Namespace) -> int:
    """Show what the model chose for a probe's placeholders, per run.

    Worth checking when drift looks implausible: a model that picked a
    different root this run manufactured the entire diff.
    """
    _, root = _load(args)
    probe_id = args.probe
    try:
        with get_db(root) as conn:
            rows = db_store.parameter_history(
                conn, probe_id, limit=int(getattr(args, "limit", 20) or 20)
            )
    except SchemaMismatchError as e:
        logger.error(str(e))
        return 2

    if not rows:
        logger.error("No recorded runs for probe {!r}.", probe_id)
        return 2

    print(f"Parameters chosen for {probe_id}, newest first")
    previous: str | None = None
    for run_id, at, params_json in rows:
        params = json.loads(params_json)
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(params.items()))
        changed = " <- CHANGED" if previous is not None and rendered != previous else ""
        print(f"  run {run_id:>4}  {at}  {rendered}{changed}")
        previous = rendered
    return 0


def _record_gate(root: Path, report: GateReport) -> None:
    """Record one gate's findings, without letting the record break the gate.

    The gates kept no history at all before this, which is what made
    "when did this key first appear" an argument rather than a query. But
    a database that needs a migration must not turn a clean licence check
    into a failure — so this warns, names the command that fixes it, and
    leaves the exit code alone. The verdict is the gate's job; the record
    is what makes the verdict worth having next month.
    """
    from vibe_sentinel.db import gate_store
    from vibe_sentinel.schemas import GateState

    try:
        with get_db(root) as conn:
            gate_store.save_gate_state(
                conn, GateState(reports=(report,)), root=root.as_posix()
            )
    except SchemaMismatchError as e:
        logger.warning("{} — this run was not recorded.", e)
    except Exception as e:  # noqa: BLE001 - a record must not break a check
        logger.warning(
            "could not record the {} gate ({}): the check above still stands, "
            "but no history row was written. Try `vibe-sentinel db check`.",
            report.gate,
            e,
        )


def run_licenses(args: argparse.Namespace) -> int:
    """Check dependency licences. 0 = clean, 1 = violations, 2 = error."""
    import importlib.metadata as md

    from vibe_sentinel.gates import shape_licenses
    from vibe_sentinel.licenses import (
        CATEGORIES,
        check,
        evaluate_expression,
        load_policy,
        project_license,
        scan_source,
    )

    config, root = _load(args)
    policy_arg = getattr(args, "policy", None)
    path = Path(policy_arg) if policy_arg else None

    if getattr(args, "list_categories", False):
        print("Licence categories, by the obligation they create:\n")
        # Show the EFFECTIVE table, so a category the config defined appears
        # here rather than only in a gate run. A missing or broken policy must
        # not stop the built-ins printing: this is the command you run to find
        # out what to write, which is exactly when there is nothing to load.
        table = CATEGORIES
        try:
            table = load_policy(path, root=root).category_map or CATEGORIES
        except (OSError, KeyError, ValueError) as e:
            logger.debug("listing built-in categories only: {}", e)
        for name, members in table.items():
            mine = " (defined by your policy)" if name not in CATEGORIES else ""
            print(f"  {name}{mine}")
            print(f"    {', '.join(sorted(members))}\n")
        print(
            "Use in .vibe-sentinel.toml:\n"
            "  [licenses]\n"
            '  allowed_categories = ["permissive", "public-domain"]\n\n'
            "A licence in no category matches nothing, so it is rejected until\n"
            "listed in allowed_spdx — absence is never treated as permissive.\n\n"
            "Add one of your own with [licenses.categories]; reusing a built-in\n"
            "name extends it rather than replacing it."
        )
        return 0

    try:
        policy = load_policy(path, root=root)
    except FileNotFoundError as e:
        logger.error(str(e))
        return 2
    except (KeyError, ValueError) as e:
        logger.error("Licence policy is invalid: {}", e)
        return 2

    # Where a NEW pin belongs: the last layer, which is the project's own.
    policy_source = policy.sources[-1] if policy.sources else str(path)

    explain_name = getattr(args, "explain", None)
    if explain_name:
        return _explain_package(explain_name, policy, policy_source, config, args)

    if len(policy.sources) > 1:
        # Which rules are actually in force, when more than one file says.
        print(f"policy:    {' + '.join(policy.sources)}")
    if policy.categories:
        print(f"accepting: {', '.join(policy.categories)}")
    custom = sorted(set(policy.category_map) - set(CATEGORIES))
    if custom:
        print(f"categories defined here: {', '.join(custom)}")
    if policy.markers:
        print(
            f"{len(policy.markers)} licence fingerprint(s) from config: "
            f"{', '.join(sorted({m.spdx for m in policy.markers}))}"
        )

    ok, bad = check(list(md.distributions()), policy)

    # The codebase's own licences. A vendored file arrives with a header and
    # never touches pyproject.toml, so no dependency gate would ever see it.
    own = project_license(root, policy.all_markers())
    if own:
        print(f"project licence: {own.spdx} (from {own.path})")
    in_source = scan_source(root, markers=policy.all_markers())
    unacceptable = [
        found
        for found in in_source
        if not evaluate_expression(found.spdx, policy.allowed, policy.exceptions)
    ]
    if in_source:
        print(f"{len(in_source)} source file(s) carry their own licence header:")
        for found in in_source:
            flag = "  <- NOT ACCEPTED" if found in unacceptable else ""
            category = policy.category_of(found.spdx) or "uncategorised"
            print(
                f"  {found.path}: {found.spdx} ({category}, via {found.source}){flag}"
            )
    print()

    if getattr(args, "verbose", False):
        for res in sorted(ok, key=lambda r: r.name.lower()):
            print(f"  {res.name:<34} {res.spdx:<28} (via {res.source})")

    _record_gate(
        root,
        shape_licenses(
            ok_count=len(ok),
            bad=bad,
            in_source=in_source,
            accepted=lambda spdx: evaluate_expression(
                spdx, policy.allowed, policy.exceptions
            ),
            policy=policy,
            project_spdx=own.spdx if own else "",
        ),
    )

    if unacceptable:
        print(
            f"{len(unacceptable)} file(s) inside this codebase carry a licence that "
            f"is not accepted. Vendored code brings its terms with it — remove the "
            f"file, replace it, or accept the obligation deliberately."
        )
        return 1

    if bad:
        print(f"{len(bad)} package(s) failed the licence gate:")
        for violation in sorted(bad, key=lambda v: v.resolved.name.lower()):
            res = violation.resolved
            print(f"  {res.name}:{res.version} — {violation.why} (via {res.source})")
        print(
            "\nVerify the licence from the LICENSE file each package ships — not "
            "from PyPI metadata, not from memory — then record it in "
            f"{policy_source}:\n"
            "\n"
            "  [[licenses.pin]]\n"
            '  packages = ["<name>"]\n'
            '  accept = ["<what you verified>"]\n'
            '  reason = """why this is acceptable here"""\n'
            '  verified = "<today>"\n'
            "\n"
            "Or widen the policy — see: vibe-sentinel licenses --list-categories"
        )
        return 1

    print(f"checked {len(ok)} packages, all permissive or explicitly pinned")
    return 0


def _spdx_terms(expr: str) -> list[str]:
    """The identifiers in an expression, without the operators."""
    from vibe_sentinel.licenses import _tokenize

    return [
        t
        for t in _tokenize(expr)
        if t not in ("(", ")") and t.upper() not in ("AND", "OR", "WITH")
    ]


def _find_distribution(name: str) -> md.Distribution | None:
    """The installed distribution called ``name``, matched the way pip does.

    ``importlib.metadata.distribution`` is exact; a user typing the name off a
    gate failure has no reason to know whether it was ``pip_audit`` or
    ``pip-audit``.
    """
    import importlib.metadata as md

    want = name.lower().replace("_", "-")
    for dist in md.distributions():
        meta_name = dist.metadata and dist.metadata.get("Name")
        if meta_name and meta_name.lower().replace("_", "-") == want:
            return dist
    return None


def _explain_package(
    name: str,
    policy: Policy,
    policy_source: str,
    config: SentinelConfig,
    args: argparse.Namespace,
) -> int:
    """Show every step of the chain for one package. 0 = passes, 1 = fails.

    Deliberately reports the gate's own verdict rather than always succeeding:
    a diagnostic that says "fine" about a package the gate rejects is a
    diagnostic someone will eventually run in CI by mistake.
    """
    import asyncio
    from datetime import UTC, datetime

    from vibe_sentinel.exceptions import LLMConnectionError
    from vibe_sentinel.llm import check_endpoint
    from vibe_sentinel.licenses import (
        check,
        draft_explanation,
        draft_pin,
        explain,
    )

    dist = _find_distribution(name)
    if dist is None:
        logger.error(
            "No installed distribution named {!r}. The gate reads what is "
            "INSTALLED, not what is declared — check the name with: pip show {}",
            name,
            name,
        )
        return 2

    resolved, evidence = explain(dist, policy.all_markers())
    print(f"{resolved.name} {resolved.version}\n")
    print("resolution chain — every step, not only the one that answered:")
    last_step = ""
    for item in evidence:
        label = "" if item.step == last_step else item.step
        last_step = item.step
        mark = "*" if item.used else " "
        print(f" {mark} {label:<19} {item.detail[:86]}")
        if item.identifiers:
            print(f" {'':<21} -> {', '.join(item.identifiers)}")
        elif not item.detail.startswith("("):
            # Saw a real declaration and matched nothing. Worth stating: it is
            # the difference between "nobody said" and "we could not read it".
            print(f" {'':<21} -> nothing identified")
    print("\n  * = the step resolve() took its answer from\n")

    # A conjunction has no single category, and printing "uncategorised" for one
    # reads as a gap in the table rather than as what it is: several licences,
    # each with its own obligation, all of which apply.
    terms = _spdx_terms(resolved.spdx)
    if len(terms) > 1:
        described = ", ".join(
            f"{t} ({policy.category_of(t) or 'uncategorised'})" for t in terms
        )
        print(f"resolved: {resolved.spdx}  (via {resolved.source})")
        print(f"          every term applies: {described}")
    else:
        category = policy.category_of(resolved.spdx) or "uncategorised"
        print(f"resolved: {resolved.spdx}  ({category}, via {resolved.source})")

    ok, bad = check([dist], policy)
    pin = policy.pin_for(resolved.name)
    if ok and pin is not None:
        print(f"policy:   ACCEPTED by an existing pin in {policy_source}")
        print(
            f"          reason on record: {' '.join(str(pin['reason']).split())[:200]}"
        )
    elif ok:
        print(f"policy:   ACCEPTED — {resolved.spdx} is within the allow-list")
    for violation in bad:
        print(f"policy:   REJECTED — {violation.why}")

    draft = None
    if getattr(args, "no_model", False):
        print("\nmodel:    skipped (--no-model)")
    else:
        # Probe first. llm_timeout is 300s by default, which is right for a
        # scan and wrong for a diagnostic someone is watching: a down backend
        # would look like a hang. check_endpoint gives up in ten seconds.
        reachable, detail = asyncio.run(check_endpoint(config))
        if not reachable:
            print(
                f"\nmodel:    unreachable ({detail}) — start it with "
                f"'vibe-sentinel backend start'. Everything above is unaffected: "
                f"the chain never uses a model."
            )
        else:
            try:
                draft = asyncio.run(
                    draft_explanation(
                        resolved, evidence, config, guidance=policy.guidance
                    )
                )
            except LLMConnectionError as e:
                logger.warning("model went away mid-request: {}", e)
            if draft is None:
                print("\nmodel:    no usable answer — the draft below is a blank")

    if draft is not None:
        # Printed as a suggestion and nothing more. It did not produce the
        # verdict above and must not read as though it had.
        print("\nmodel suggestion — UNVERIFIED, this is not a review:")
        if policy.guidance:
            # Say that the draft was steered, so nobody wonders why it reads
            # the way it does — or forgets the guidance is there to update.
            print(f"  house guidance applied, from {policy.sources[-1]}")
        print(
            f"  reads the evidence as: {draft.identifier or '(cannot tell)'} "
            f"(confidence: {draft.confidence})"
        )
        if draft.identifier and draft.identifier != resolved.spdx:
            print(
                f"  NOTE: differs from the resolver's {resolved.spdx}. The "
                f"resolver decides; this disagreement is a reason to read the "
                f"licence file yourself."
            )
        if draft.verify:
            print(f"  before signing: {draft.verify}")

    if not bad:
        # No pin is needed, so drafting one is noise — and where a pin already
        # exists, drafting a replacement invites someone to paste over a reason
        # a human wrote and dated.
        print(
            f"\nA pin for {resolved.name} is already on record and still matches."
            if pin is not None
            else f"\n{resolved.name} needs no pin: the policy accepts "
            f"{resolved.spdx} outright."
        )
        return 0

    reason = (draft.reason if draft else "") or "why this is acceptable HERE — "
    if not (draft and draft.reason):
        reason += "name the boundary: subprocess, dev-only, unmodified, not shipped"
    print(f"\ndraft pin for {policy_source}:\n")
    print(draft_pin(resolved, evidence, reason, datetime.now(UTC).date().isoformat()))
    print(
        "\nVerify against the LICENSE file the package ships — not PyPI, not "
        "memory, not the draft above — then edit the reason and commit it."
    )
    return 1 if bad else 0


def run_packages(args: argparse.Namespace) -> int:
    """Check dependency provenance. 0 = clean, 1 = findings, 2 = error.

    Every check is mechanical bar one. Two installed names an edit apart
    is a typosquat and a rewrite shipped under a new name in the same
    shape, so that one pair — and nothing else here — is put to the model.
    ``--no-model`` skips it, and every near-miss then stands exactly as it
    did before the step existed.
    """
    import asyncio

    from vibe_sentinel.gates import shape_packages
    from vibe_sentinel.packages import (
        adjudicate,
        audit,
        by_severity,
        confirm_online,
        load_policy,
        names_to_confirm,
        registry_facts,
        take_inventory,
    )

    config, root = _load(args)
    use_model = not getattr(args, "no_model", False)
    policy_arg = getattr(args, "policy", None)

    try:
        policy = load_policy(Path(policy_arg) if policy_arg else None, root=root)
    except FileNotFoundError as e:
        logger.error(str(e))
        return 2
    except (KeyError, ValueError) as e:
        logger.error("Package policy is invalid: {}", e)
        return 2

    inventory = take_inventory(root)
    env = inventory.environment

    # Which environment was read is the first thing on screen, not a footnote.
    # Every number below is a statement about this interpreter and no other, and
    # a check silently run against the wrong env is worse than no check.
    print(f"environment: {env.label} — Python {env.python} at {env.prefix}")
    print(f"policy: {policy.source}")
    print(
        f"{len(inventory.installed)} package(s) installed, "
        f"{len(inventory.direct)} declared, "
        f"{len(inventory.external_imports)} external import(s) in source"
    )

    findings = audit(inventory, policy)

    online = bool(getattr(args, "online", False))
    if online:
        names = names_to_confirm(inventory, findings)
        print(f"asking the index about {len(names)} name(s)...")
        findings = confirm_online(inventory, findings, registry_facts(names), policy)
    else:
        print(
            "index not queried (--online is off), so a name reported as "
            "'phantom' has not been checked against PyPI"
        )
    print()

    findings = by_severity(findings)
    adjudged = asyncio.run(
        adjudicate(inventory, findings, policy, config, use_model=use_model)
    )
    _record_gate(
        root,
        shape_packages(inventory, findings, online=online, adjudication=adjudged),
    )

    if getattr(args, "verbose", False):
        flagged = {f.name for f in findings}
        for name in sorted(inventory.installed):
            dist = inventory.installed[name]
            where = "declared" if name in inventory.direct else "transitive"
            mark = "  <- flagged" if name in flagged else ""
            print(f"  {name:<34} {dist.version:<14} {where}{mark}")
        print()

    if not findings:
        print("no provenance findings — every import resolves and every")
        print("installed package traces back to something declared")
        return 0

    judged = {j.finding.name: j for j in adjudged.judgements}
    standing = {f.name for f in adjudged.failing()}

    print(f"{len(findings)} provenance finding(s):")
    for finding in findings:
        settled = "" if finding.name in standing else "  (settled)"
        print(f"\n  [{finding.kind}] {finding.name}{settled}")
        print(f"      {finding.detail}")
        for item in finding.evidence[:8]:
            print(f"      · {item}")
        judgement = judged.get(finding.name)
        if judgement is not None and judgement.verdict != "unreviewed":
            # Said as a verdict rather than folded into the detail: which
            # of the two names is the imposter is the model's opinion, and
            # a reader has to be able to see that it is one.
            who = f" ({judgement.suspect})" if judgement.suspect else ""
            print(f"      model: {judgement.verdict}{who} — {judgement.reason}")
        elif judgement is not None:
            print(f"      model: not adjudicated — {judgement.reason or 'not asked'}")
        print(f"      -> {finding.remediation}")

    if adjudged.note:
        print(f"\n{adjudged.note}")
    print(
        "\nRecord a decision you have actually made as a [[packages.pin]] in "
        ".vibe-sentinel.toml,\nnaming the finding kinds it accepts. A pin is "
        "scoped: accepting 'orphan' for a package\ndoes not accept 'squatted' "
        "for it later."
    )
    return 1 if standing else 0


def run_credentials(args: argparse.Namespace) -> int:
    """Check for credentials at rest. 0 = clean, 1 = findings, 2 = error.

    Two stages: a pattern match over paths and content, then the local
    model on what it flagged. ``--no-model`` stops after the first, which
    is the CI path — the candidates are listed, plainly marked as
    adjudicated by nobody.
    """
    import asyncio

    from vibe_sentinel import credentials as creds
    from vibe_sentinel.gates import shape_credentials

    config, root = _load(args)

    try:
        secrets = creds.load_secrets(root)
    except ValueError as e:
        logger.error(str(e))
        return 2

    if getattr(args, "print_rules", False):
        render_secret_rules(secrets)
        return 0

    policy_arg = getattr(args, "policy", None)
    try:
        policy = creds.load_policy(Path(policy_arg) if policy_arg else None, root=root)
    except FileNotFoundError as e:
        logger.error(str(e))
        return 2
    except (KeyError, ValueError) as e:
        logger.error("Credential policy is invalid: {}", e)
        return 2

    home = bool(getattr(args, "home", False))
    if home:
        print(
            "including the home-directory credential stores. Nothing there was "
            "ever\ncovered by a .gitignore, and an agent reads it with `cat`.\n"
        )
    scan = creds.collect(root, secrets, policy, home=home)

    findings = asyncio.run(
        creds.adjudicate(
            scan,
            secrets,
            policy,
            config,
            use_model=not getattr(args, "no_model", False),
        )
    )
    _record_gate(root, shape_credentials(scan, findings, policy))
    render_credentials(findings, policy)
    return 1 if findings.failing(policy) else 0


def run_db(args: argparse.Namespace) -> int:
    """Dispatch ``vibe-sentinel db`` subcommands.

    Everything here is upkeep on the history file — its size, its health,
    a copy of it, a trim of it. None of it rebuilds the database, and the
    two that remove anything (``prune``, ``vacuum``) take a backup first
    without being asked.
    """
    from vibe_sentinel.db import maintenance

    config, root = _load(args)
    action = getattr(args, "db_action", None) or "status"
    path = db_path(root)

    if not path.is_file():
        logger.error(
            "No history database at {}. `vibe-sentinel scan` creates one.", path
        )
        return 2

    # backup, vacuum and reindex work on the file directly and are the
    # things you reach for when a database is in a state get_db refuses
    # to open. They must not be gated behind opening it.
    if action == "backup":
        output = getattr(args, "output", None)
        try:
            result = maintenance.backup(root, Path(output) if output else None)
        except OSError as e:
            logger.error("Backup failed: {}", e)
            return 2
        print(f"Backed up to {result.path}")
        print(f"  {result.size_bytes / 1024:.1f} KB in {result.duration_ms} ms")
        print("  To restore, copy that file back over the database.")
        try:
            with get_db(root) as conn:
                maintenance.record(
                    conn,
                    "backup",
                    ok=True,
                    size=maintenance.measure(conn, path),
                    detail=str(result.path),
                    duration_ms=result.duration_ms,
                )
        except SchemaMismatchError:
            # The backup is what matters and it is already on disk; a
            # database this build cannot open cannot be told about it.
            logger.debug("backup taken but not recorded — schema mismatch")
        return 0

    if action == "vacuum":
        before, after = maintenance.vacuum(root)
        saved = before - after
        print(f"Vacuumed {path}")
        print(f"  {before / 1024:.1f} KB -> {after / 1024:.1f} KB")
        print(
            f"  reclaimed {saved / 1024:.1f} KB"
            if saved > 0
            else "  nothing to reclaim; the file was already compact."
        )
        try:
            with get_db(root) as conn:
                maintenance.record(
                    conn,
                    "vacuum",
                    ok=True,
                    size=maintenance.measure(conn, path),
                    detail=f"{before} -> {after} bytes",
                )
        except SchemaMismatchError:
            # Same as backup's: the vacuum is done and the file is already
            # compact. A database this build cannot open cannot be told
            # about it, and this is one of the commands you reach for
            # precisely because it will not open.
            logger.debug("vacuum done but not recorded — schema mismatch")
        return 0

    if action == "reindex":
        created = maintenance.reindex(root)
        if not created:
            print("Every declared index is present — nothing to rebuild.")
            return 0
        print(f"Rebuilt {len(created)} index(es):")
        for name in created:
            print(f"  {name}")
        return 0

    try:
        with get_db(root) as conn:
            if action == "status":
                render_db_status(
                    maintenance.measure(conn, path),
                    maintenance.recent_maintenance(conn),
                )
                return 0

            if action == "check":
                report = maintenance.check(conn, root, config, full=True)
                maintenance.record(
                    conn,
                    "health",
                    ok=report.ok,
                    size=report.size,
                    findings=report.findings,
                    duration_ms=report.duration_ms,
                )
                if getattr(args, "format", None) == "json":
                    print(report.model_dump_json(indent=2))
                else:
                    render_db_health(report)
                # 1, not 2: findings are a result, not a failure to run.
                return 1 if report.attention else 0

            if action == "prune":
                return _run_prune(args, conn, root, config)
    except SchemaMismatchError as e:
        logger.error(str(e))
        return 2

    logger.error("vibe-sentinel db: no action given. Try 'db status'.")
    return 2


def _run_prune(
    args: argparse.Namespace,
    conn: sqlite3.Connection,
    root: Path,
    config: SentinelConfig,
) -> int:
    """Trim old records. Counts them unless ``--apply`` is given."""
    from vibe_sentinel.db import maintenance

    before = getattr(args, "before", None)
    older_than = getattr(args, "older_than", None)

    if before and older_than:
        logger.error("Give --before or --older-than, not both.")
        return 2
    if before:
        cutoff = str(before)
    elif older_than:
        cutoff = maintenance.cutoff_from_days(int(older_than))
    elif config.db_journal_retention_days:
        cutoff = maintenance.cutoff_from_days(config.db_journal_retention_days)
        print(
            f"Using the {config.db_journal_retention_days}-day retention "
            f"declared in [database] journal_retention_days."
        )
    else:
        logger.error(
            "No cutoff. Pass --older-than DAYS or --before DATE, or declare "
            "one once in .vibe-sentinel.toml:\n"
            "  [database]\n  journal_retention_days = 90"
        )
        return 2

    scans = bool(getattr(args, "scans", False))
    apply = bool(getattr(args, "apply", False))

    if scans and not apply:
        print(
            "NOTE: --scans includes the structural history. Those runs cannot "
            "be re-measured — probes re-run against today's code, not the code "
            "as it was. The baseline and the newest runs are never deleted, "
            "and an applied prune backs up first.\n"
        )

    result = maintenance.prune(
        conn,
        root,
        cutoff=cutoff,
        scans=scans,
        keep_runs=int(getattr(args, "keep_runs", 10) or 0),
        apply=apply,
    )
    render_prune(result)

    if result.applied:
        maintenance.record(
            conn,
            "prune",
            ok=True,
            size=maintenance.measure(conn, db_path(root)),
            detail=(
                f"cutoff {cutoff}; removed "
                + ", ".join(f"{n} {t}" for t, n in result.deleted.items() if n)
            ),
        )
    return 0


def run_backend(args: argparse.Namespace) -> int:
    """Dispatch ``vibe-sentinel backend`` subcommands."""
    import asyncio

    from vibe_sentinel import backend

    config_arg = getattr(args, "config", None)
    config = load_config(Path(config_arg) if config_arg else None)
    action = getattr(args, "backend_action", None)

    if action == "status":
        return asyncio.run(backend.status(config))
    if action == "start":
        return asyncio.run(
            backend.start(config, wait=not getattr(args, "no_wait", False))
        )
    if action == "stop":
        return backend.stop(config)

    logger.error("vibe-sentinel backend: no action given. Try 'backend status'.")
    return 2


def run_hook(args: argparse.Namespace) -> int:
    """Record one tool call, or manage the hook that sends them.

    With no flag this is the hook itself: it reads one Claude Code
    payload from stdin and returns 0 whatever happens. A ``PreToolUse``
    hook's exit code is how Claude Code is told to *block* a tool call,
    and this has no opinion about what the agent is  about to run — it
    records. Nothing is written to stdout either, for the same reason:
    stdout is where a hook puts a permission decision.
    """
    from vibe_sentinel import hook

    matcher = getattr(args, "matcher", None) or hook.DEFAULT_MATCHER

    if getattr(args, "print_config", False):
        entry = hook.settings_entry(matcher=matcher)
        print(json.dumps({"hooks": {"PreToolUse": [entry]}}, indent=2))
        return 0

    if getattr(args, "install", False):
        _, root = _load(args)
        try:
            path, changed = hook.install(root, matcher=matcher)
        except (OSError, ValueError) as e:
            logger.error(str(e))
            return 2
        if changed:
            print(f"PreToolUse hook installed in {path}")
            print("It takes effect in Claude Code sessions started from now on.")
        else:
            print(f"PreToolUse hook already installed in {path}")
        return 0

    if getattr(args, "replay", False):
        _, root = _load(args)
        try:
            recorded, kept = hook.replay_spill(root)
        except SchemaMismatchError as e:
            logger.error(str(e))
            return 2
        except (sqlite3.Error, OSError) as e:
            logger.error(
                "Could not drain {}: {}. The spill file is unchanged.",
                hook.spill_path(root),
                e,
            )
            return 2
        print(f"replayed {recorded} event(s) into {db_path(root)}")
        if kept:
            print(f"{kept} unreadable event(s) left in {hook.spill_path(root)}")
        return 0

    outcome, decision = hook.guard(sys.stdin.read())
    if decision is not None:
        print(json.dumps(decision))
    logger.debug("hook: {}", outcome)
    return 0


def run_commands(args: argparse.Namespace) -> int:
    """List what the coding agent ran, as the hook recorded it."""
    _, root = _load(args)
    limit = int(getattr(args, "limit", 50) or 50)
    session = getattr(args, "session", None)
    agent = getattr(args, "agent", None)
    # `--agent main` names the top-level thread, whose agent_id is the
    # empty string. Without this you could not ask for "the session's own
    # commands, not its subagents'".
    agent_id = "" if agent == "main" else agent

    try:
        with get_db(root) as conn:
            if getattr(args, "fields", False):
                scanned, fields = journal_store.observed_fields(conn)
                render_observed_fields(scanned, fields)
                return 0

            if getattr(args, "sessions", False):
                render_actors(
                    journal_store.list_agent_sessions(
                        conn, limit=limit, session_id=session
                    )
                )
                return 0

            if getattr(args, "tools", False):
                render_tool_counts(journal_store.tool_counts(conn, session_id=session))
                return 0

            run_id = getattr(args, "run", None)
            if run_id:
                commands = journal_store.commands_for_run(
                    conn, int(run_id), limit=limit
                )
            else:
                commands = journal_store.list_commands(
                    conn,
                    limit=limit,
                    session_id=session,
                    agent_id=agent_id,
                    agent_type=getattr(args, "agent_type", None),
                    prompt_id=getattr(args, "prompt", None),
                    tool_name=getattr(args, "tool", None),
                )

            if not commands and journal_store.journal_totals(conn)[2] == 0:
                logger.error(
                    "No commands recorded in {}. Install the hook with: "
                    "vibe-sentinel hook --install",
                    db_path(root),
                )
                return 2

            if (getattr(args, "format", None) or "terminal") == "json":
                render_commands_json(commands)
            else:
                render_commands(commands)
    except SchemaMismatchError as e:
        logger.error(str(e))
        return 2
    return 0


def run_safety(args: argparse.Namespace) -> int:
    """Show recorded verdicts, or try the gate on one command.

    ``--check`` is the important one. Tuning a gate by waiting for an
    agent to run something dangerous is not tuning; this runs the same
    triage and the same review, against this project's real recent
    history, on a command you type. Nothing is blocked, and the verdict
    is recorded — see :func:`_record_finding` for why, and for what keeps
    it from reading as work an agent did.
    """
    import asyncio

    from vibe_sentinel import safety

    config, root = _load(args)
    command = getattr(args, "check", None)

    pattern = getattr(args, "match", None)
    if pattern:
        import re

        try:
            candidate = re.compile(pattern, re.I)
        except re.error as e:
            logger.error(
                "{!r} is not a valid regular expression: {}. Patterns are "
                "regexes over the command text.",
                pattern,
                e,
            )
            return 2
        try:
            dangers = safety.load_dangers(root)
            with get_db(root) as conn:
                recorded = journal_store.list_commands(conn, limit=10_000)
        except (SchemaMismatchError, ValueError) as e:
            logger.error(str(e))
            return 2
        matched = [
            (
                row.command or row.describe(),
                bool(
                    safety.triage(row.tool_name, row.command, row.target, root, dangers)
                ),
            )
            for row in reversed(recorded)
            if candidate.search(row.command or row.describe())
        ]
        render_pattern_match(pattern, len(recorded), matched)
        return 0

    if getattr(args, "print_dangers", False):
        try:
            render_dangers(safety.load_dangers(root))
        except ValueError as e:
            logger.error(str(e))
            return 2
        return 0

    try:
        with get_db(root) as conn:
            if command:
                dangers = safety.load_dangers(root)
                signals = safety.triage("Bash", command, "", root, dangers)
                render_triage(command, signals)
                if not signals:
                    return 0

                actors = journal_store.list_agent_sessions(conn, limit=1)
                history = (
                    journal_store.recent_commands_for_actor(
                        conn,
                        actors[0].session_id,
                        actors[0].agent_id,
                        limit=config.safety_history,
                    )
                    if actors
                    else []
                )
                print(
                    f"  history: {len(history)} command(s) from {actors[0].actor if actors else 'nobody'}"
                )

                if getattr(args, "show_prompt", False):
                    print("\n--- shared prefix (system; cached across the fan-out) ---")
                    print(
                        safety.build_prompt(
                            "Bash",
                            command,
                            "",
                            str(root),
                            root,
                            signals,
                            list(history),
                            dangers,
                        )
                    )
                    for danger in safety.questions_for(signals, dangers):
                        print("\n--- one request (user) ---")
                        print(safety.build_question(danger))
                    print("--- end ---\n")

                settled = safety.declared_verdict(signals, dangers)
                if settled is not None:
                    verdict, reason = settled
                    print(f"\n  verdict: {verdict}   (declared; no model asked)")
                    print(f"  reason:  {reason}")
                    _record_finding(root, command, signals, verdict, reason, "", False)
                    return 1 if verdict == "unsafe" else 0

                if getattr(args, "no_model", False):
                    print("  --no-model: not asking for a verdict.")
                    return 0

                opinion = asyncio.run(
                    safety.review(
                        "Bash",
                        command,
                        "",
                        str(root),
                        root,
                        signals,
                        list(history),
                        config,
                        dangers,
                    )
                )
                if opinion is None:
                    logger.error(
                        "The model did not answer, so this command has no verdict. "
                        "Check it with: vibe-sentinel backend status"
                    )
                    _record_finding(root, command, signals, "unreviewed", "", "", False)
                    return 2
                print(f"\n  verdict: {opinion.verdict}")
                if opinion.resolves_to:
                    print(f"  resolves to: {opinion.resolves_to}")
                print(f"  reason:  {opinion.reason}")
                _record_finding(
                    root,
                    command,
                    signals,
                    opinion.verdict,
                    opinion.reason,
                    config.llm_model,
                    True,
                )
                return 1 if opinion.verdict == "unsafe" else 0

            rows = journal_store.list_reviews(
                conn,
                limit=int(getattr(args, "limit", 50) or 50),
                verdict=getattr(args, "verdict", None),
                session_id=getattr(args, "session", None),
            )
            render_reviews(rows)
            if config.safety_mode == "off" and not rows:
                print(
                    "\nThe gate is off. Turn it on in .vibe-sentinel.toml:\n"
                    '  [safety]\n  mode = "observe"   # record verdicts, block nothing'
                )
    except SchemaMismatchError as e:
        logger.error(str(e))
        return 2
    return 0


def _record_finding(
    root: Path,
    command: str,
    signals: tuple[str, ...],
    verdict: str,
    reason: str,
    model: str,
    reviewed: bool,
) -> None:
    """Store what a ``--check`` found, the same as a real verdict.

    A verdict reached while tuning is still a verdict about a command,
    and the history is the product here: a finding that only ever reached
    a terminal is one nobody can point at next week.

    Three things keep it from reading as work an agent did, and all three
    have to hold together — storing the distinction is not the same as
    showing it, and for a while only the storing was true. It gets its
    own ``session_id``; it is marked ``mode = "check"``, which
    :func:`~vibe_sentinel.report.render_reviews` prints as ``[check]``;
    and it declares ``agent_type = "check"``, which is what
    :func:`~vibe_sentinel.journal._actor_name` renders instead of
    ``main`` in every listing.
    """
    from vibe_sentinel.hook import now_iso
    from vibe_sentinel.journal import HookEvent

    occurred_at = now_iso()
    try:
        with get_db(root) as conn:
            command_id = journal_store.record_command(
                conn,
                HookEvent(
                    hook_event_name="Check",
                    session_id="vibe-sentinel-check",
                    agent_type="check",
                    cwd=str(root),
                    tool_name="Bash",
                    tool_input={"command": command},
                ),
                occurred_at,
            )
            if command_id is None:
                return
            journal_store.save_review(
                conn,
                command_id=command_id,
                reviewed_at=occurred_at,
                signals=",".join(signals),
                verdict=verdict,
                reason=reason,
                model=model,
                reviewed=reviewed,
                mode="check",
                enforced=False,
                history_count=0,
                duration_ms=0,
            )
    except (SchemaMismatchError, sqlite3.Error, OSError) as e:
        logger.warning("could not record this check: {}", e)
