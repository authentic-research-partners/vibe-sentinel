"""CLI entry point: ``vibe-sentinel scan``

Surfaces:

- ``scan``       — run the probes, record the structure, report drift.
- ``history``    — list recorded runs, or show one in detail.
- ``trend``      — the recorded history, fitted: slopes and anomalies.
- ``parameters`` — what the model chose for a probe's placeholders.
- ``migrate``    — update the history database's schema.
- ``backups``    — list every copy of the database, however it was made.
- ``db``         — size, health, backup, prune, vacuum the history file.
- ``hook``       — record one agent tool call; install the hook.
- ``commands``   — what the coding agent ran, and which actor ran it.
- ``safety``     — verdicts on those commands, or try the gate on one.
- ``licenses``   — check dependency licences against the policy.
- ``packages``   — check dependency provenance: does it exist, who added it.
- ``backend``    — start, stop, or check the model server.

Command implementations live in :mod:`vibe_sentinel.cli`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from vibe_sentinel.log import logger

from vibe_sentinel import __version__

if TYPE_CHECKING:
    from vibe_sentinel.config import SentinelConfig


def _setup_logging(config: SentinelConfig) -> None:
    """Add a rotating file sink at the configured level."""
    logger.add(
        "logs/vibe-sentinel.log",
        rotation="10 MB",
        retention="30 days",
        compression="gz",
        level=config.log_level,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | "
            "{name}:{function}:{line} | {message}"
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibe-sentinel",
        description=(
            "Guards structural expectations about how a codebase is "
            "organized, and reports drift."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"vibe-sentinel {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    # --- scan ---
    p_scan = sub.add_parser(
        "scan", help="Run the probes, record the structure, report drift"
    )
    p_scan.add_argument(
        "root",
        nargs="?",
        default=None,
        help="Project root to scan (default: the config's directory, else cwd)",
    )
    p_scan.add_argument(
        "--probes",
        default=None,
        metavar="ID[,ID...]",
        help="Run only the listed probe IDs. Default: every probe in the config.",
    )
    p_scan.add_argument(
        "--update",
        action="store_true",
        help="Record this scan as the new baseline. Off by default so a "
        "drift check never silently rewrites the baseline it just failed "
        "against — accepting a structural change should be deliberate.",
    )
    p_scan.add_argument(
        "--since",
        default=None,
        metavar="HORIZON[,HORIZON...]",
        help="Report what moved over these horizons too, instead of the "
        "[drift] horizons the config declares. Units: h, d, w, m (30 "
        "days), y — so `--since 1w,3m`. Each is compared against the "
        "newest run at least that old, because drift slow enough to stay "
        "under every tolerance between two scans is still a "
        "reorganization by the end of a month. A horizon never moves the "
        "baseline and never changes the exit code: it is a reading, not "
        "a verdict. `--since ''` turns them off for this run.",
    )
    p_scan.add_argument(
        "--fit",
        type=int,
        default=None,
        metavar="RUNS",
        help="Fit each observation's recorded history over this many runs "
        "instead of [drift] trend_runs; 0 skips the fits. A horizon "
        "compares two points, and a direction is a property of all of "
        "them — the fit is a Theil–Sen slope with a Mann–Kendall "
        "significance test, and the value this scan measured is scored "
        "against a fit made without it. Like a horizon it changes no "
        "exit code and moves no baseline.",
    )
    p_scan.add_argument(
        "--no-model",
        action="store_true",
        help="Skip the local model entirely: fill placeholders from their "
        "declared defaults and report drift without the significance "
        "analysis. The path for CI or a machine with no GPU.",
    )
    p_scan.add_argument(
        "--list-probes",
        action="store_true",
        help="Print the active probe set and exit.",
    )
    p_scan.add_argument(
        "--print-example",
        action="store_true",
        help="Print the package-shipped example probe set and exit. Pipe to "
        "`> .vibe-sentinel.toml` to scaffold a project.",
    )
    p_scan.add_argument(
        "--print-probes",
        action="store_true",
        dest="print_probes",
        help="Print the built-in probe set and exit. These run by default; "
        "append them to your config only to customise one.",
    )
    p_scan.add_argument(
        "--format",
        choices=["terminal", "json", "agent"],
        default=None,
        help="Output format (default: terminal). 'agent' renders drift as a "
        "constraint block to feed back to the coding agent.",
    )
    p_scan.add_argument("--config", help="Path to .vibe-sentinel.toml")

    # --- history ---
    p_hist = sub.add_parser("history", help="List recorded runs, or show one")
    p_hist.add_argument("root", nargs="?", default=None, help="Project root")
    p_hist.add_argument("--run", type=int, default=None, help="Show this run in detail")
    p_hist.add_argument("--limit", type=int, default=20, help="Runs to list")
    p_hist.add_argument(
        "-v", "--verbose", action="store_true", help="List every observation"
    )
    p_hist.add_argument("--config", help="Path to .vibe-sentinel.toml")

    # --- trend ---
    p_trend = sub.add_parser(
        "trend",
        help="Fit the recorded history — slopes, whether they mean "
        "anything, and points that left their own trend",
    )
    p_trend.add_argument("root", nargs="?", default=None, help="Project root")
    p_trend.add_argument("--probe", default=None, help="Probe id (required with --key)")
    p_trend.add_argument(
        "--key",
        default=None,
        help="Observation key: print its series with the fitted value beside "
        "each point. Omit to fit every observation at once.",
    )
    p_trend.add_argument("--limit", type=int, default=30, help="Points, with --key")
    p_trend.add_argument(
        "--runs",
        type=int,
        default=None,
        metavar="N",
        help="Runs to fit over (default: [drift] trend_runs, else 50). A "
        "direction averaged over two years is not one anybody can act on.",
    )
    p_trend.add_argument(
        "--min-runs",
        type=int,
        default=0,
        dest="min_runs",
        help="Runs an observation needs before it is fitted at all "
        "(default: 10, where the significance test becomes calibrated; "
        "below it the test is conservative rather than wrong, but three "
        "points make a line through anything). An anomaly needs 20 "
        "whatever this says — the scale one is measured in is what a "
        "short series cannot estimate.",
    )
    p_trend.add_argument("--config", help="Path to .vibe-sentinel.toml")

    # --- parameters ---
    p_params = sub.add_parser(
        "parameters",
        help="What the model chose for a probe's placeholders, per run",
    )
    p_params.add_argument("probe", help="Probe id")
    p_params.add_argument("root", nargs="?", default=None, help="Project root")
    p_params.add_argument("--limit", type=int, default=20)
    p_params.add_argument("--config", help="Path to .vibe-sentinel.toml")

    # --- migrate ---
    p_migrate = sub.add_parser(
        "migrate", help="Update the history database to this build's schema"
    )
    p_migrate.add_argument("root", nargs="?", default=None, help="Project root")
    p_migrate.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Show what would be applied without touching the database",
    )
    p_migrate.add_argument("--config", help="Path to .vibe-sentinel.toml")

    # --- backups ---
    p_backups = sub.add_parser(
        "backups", help="List database backups left by migrations"
    )
    p_backups.add_argument("root", nargs="?", default=None, help="Project root")
    p_backups.add_argument("--config", help="Path to .vibe-sentinel.toml")

    # --- db ---
    p_db = sub.add_parser(
        "db",
        help="Size, health, backup and retention for the history database",
    )
    db_sub = p_db.add_subparsers(dest="db_action")

    p_dbstatus = db_sub.add_parser(
        "status", help="What the database weighs, and when it was last checked"
    )
    p_dbstatus.add_argument("root", nargs="?", default=None, help="Project root")
    p_dbstatus.add_argument("--config", help="Path to .vibe-sentinel.toml")

    p_dbcheck = db_sub.add_parser(
        "check",
        help="Run the health check now and record it. Exit 1 when something "
        "needs attention.",
    )
    p_dbcheck.add_argument("root", nargs="?", default=None, help="Project root")
    p_dbcheck.add_argument("--format", choices=["terminal", "json"], default=None)
    p_dbcheck.add_argument("--config", help="Path to .vibe-sentinel.toml")

    p_dbbackup = db_sub.add_parser(
        "backup",
        help="Copy the database, safely, while it may be in use",
    )
    p_dbbackup.add_argument("root", nargs="?", default=None, help="Project root")
    p_dbbackup.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="Write the copy here instead of .vibe-sentinel/backups/. A copy "
        "filed by hand is never swept up by backup retention.",
    )
    p_dbbackup.add_argument("--config", help="Path to .vibe-sentinel.toml")

    p_dbprune = db_sub.add_parser(
        "prune",
        help="Delete records older than a cutoff. Counts them unless --apply.",
    )
    p_dbprune.add_argument("root", nargs="?", default=None, help="Project root")
    p_dbprune.add_argument(
        "--older-than",
        type=int,
        default=None,
        dest="older_than",
        metavar="DAYS",
        help="Cutoff as an age in days. Default: [database] "
        "journal_retention_days, if declared.",
    )
    p_dbprune.add_argument(
        "--before",
        default=None,
        metavar="TIMESTAMP",
        help="Cutoff as an absolute date, e.g. 2026-01-01",
    )
    p_dbprune.add_argument(
        "--scans",
        action="store_true",
        help="Also delete the structural history, not just the agent "
        "journal. Those runs cannot be re-measured: probes re-run against "
        "today's code. The baseline run and the newest --keep-runs are "
        "never deleted.",
    )
    p_dbprune.add_argument(
        "--keep-runs",
        type=int,
        default=10,
        dest="keep_runs",
        metavar="N",
        help="With --scans, runs to keep regardless of age (default: 10)",
    )
    p_dbprune.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without it this only counts. An applied "
        "prune backs the database up first, always — that backup is the "
        "only way back.",
    )
    p_dbprune.add_argument("--config", help="Path to .vibe-sentinel.toml")

    p_dbvacuum = db_sub.add_parser(
        "vacuum",
        help="Rewrite the file compactly to reclaim deleted pages, then "
        "refresh the query planner's statistics. Backs up first.",
    )
    p_dbvacuum.add_argument("root", nargs="?", default=None, help="Project root")
    p_dbvacuum.add_argument("--config", help="Path to .vibe-sentinel.toml")

    p_dbreindex = db_sub.add_parser(
        "reindex", help="Recreate any declared index the database is missing"
    )
    p_dbreindex.add_argument("root", nargs="?", default=None, help="Project root")
    p_dbreindex.add_argument("--config", help="Path to .vibe-sentinel.toml")

    p_db.add_argument("--config", help="Path to .vibe-sentinel.toml")

    # --- hook ---
    p_hook = sub.add_parser(
        "hook",
        help="Record one Claude Code tool call read from stdin, or install "
        "the PreToolUse hook that sends them",
    )
    p_hook.add_argument("root", nargs="?", default=None, help="Project root")
    hook_mode = p_hook.add_mutually_exclusive_group()
    hook_mode.add_argument(
        "--install",
        action="store_true",
        help="Add the PreToolUse hook to this project's .claude/settings.json "
        "(existing settings are backed up and preserved)",
    )
    hook_mode.add_argument(
        "--print-config",
        action="store_true",
        dest="print_config",
        help="Print the settings.json fragment instead of writing it",
    )
    hook_mode.add_argument(
        "--replay",
        action="store_true",
        help="Drain events that were spilled to disk while the database was "
        "unavailable — after a migration, for instance",
    )
    p_hook.add_argument(
        "--matcher",
        default=None,
        metavar="PATTERN",
        help="Which tools --install wires up: '*' for every tool call "
        "(default, the complete record), or a tool name like 'Bash' to "
        "record shell commands only and spend one process per shell "
        "command instead of one per tool call",
    )
    p_hook.add_argument("--config", help="Path to .vibe-sentinel.toml")

    # --- commands ---
    p_commands = sub.add_parser(
        "commands",
        help="What the coding agent ran, and which session or subagent ran it",
    )
    p_commands.add_argument("root", nargs="?", default=None, help="Project root")
    p_commands.add_argument(
        "--sessions",
        action="store_true",
        help="List the actors instead of their commands: one row per session "
        "and per subagent within it",
    )
    p_commands.add_argument(
        "--tools", action="store_true", help="Count calls per tool instead"
    )
    p_commands.add_argument(
        "--fields",
        action="store_true",
        help="Which hook payload fields have actually arrived carrying a "
        "value, against the set this build expects — the check on whether "
        "Claude Code still sends what the journal reads",
    )
    p_commands.add_argument("--session", default=None, help="Claude Code session id")
    p_commands.add_argument(
        "--agent",
        default=None,
        metavar="ID",
        help="Subagent id, or 'main' for the session's own commands",
    )
    p_commands.add_argument(
        "--agent-type",
        default=None,
        dest="agent_type",
        metavar="NAME",
        help="Subagent type, e.g. Explore",
    )
    p_commands.add_argument(
        "--prompt", default=None, metavar="ID", help="One turn's prompt id"
    )
    p_commands.add_argument(
        "--tool", default=None, metavar="NAME", help="Tool name, e.g. Bash"
    )
    p_commands.add_argument(
        "--run",
        type=int,
        default=None,
        metavar="N",
        help="The commands recorded between run N-1 and run N — what the "
        "agent was doing while that scan's drift appeared",
    )
    p_commands.add_argument("--limit", type=int, default=50)
    p_commands.add_argument("--format", choices=["terminal", "json"], default=None)
    p_commands.add_argument("--config", help="Path to .vibe-sentinel.toml")

    # --- licenses ---
    p_lic = sub.add_parser(
        "licenses",
        help="Check dependency licences against security/license-policy.toml",
    )
    p_lic.add_argument("root", nargs="?", default=None, help="Project root")
    p_lic.add_argument(
        "--policy",
        default=None,
        help="Use exactly this policy file, bypassing both layers. By default "
        "security/license-policy.toml is the base layer and the [licenses] "
        "table in .vibe-sentinel.toml is merged on top of it.",
    )
    p_lic.add_argument(
        "--list-categories",
        action="store_true",
        dest="list_categories",
        help="Print the licence categories and their members, then exit.",
    )
    p_lic.add_argument(
        "--explain",
        metavar="PACKAGE",
        default=None,
        help="Show every step of the resolution chain for one package, plus a "
        "draft pin. Diagnostic: it reports the same verdict the gate would.",
    )
    p_lic.add_argument(
        "--no-model",
        action="store_true",
        dest="no_model",
        help="Skip the model when explaining. The chain itself never uses one.",
    )
    p_lic.add_argument(
        "-v", "--verbose", action="store_true", help="List every resolved package"
    )
    p_lic.add_argument("--config", help="Path to .vibe-sentinel.toml")

    # --- packages ---
    p_pkg = sub.add_parser(
        "packages",
        help="Check dependency provenance: imports that resolve to nothing, "
        "packages nobody declared, names one edit apart",
    )
    p_pkg.add_argument("root", nargs="?", default=None, help="Project root")
    p_pkg.add_argument("--policy", default=None, help="Path to package-policy.toml")
    p_pkg.add_argument(
        "--online",
        action="store_true",
        help="Also ask PyPI whether the flagged names exist and how old they "
        "are. Off by default: every other check is answered from this "
        "machine, and a gate that needs the network fails in CI. Only "
        "names already flagged and the dependencies pyproject.toml states "
        "publicly are sent.",
    )
    p_pkg.add_argument(
        "--no-model",
        action="store_true",
        help="Skip the one question this gate asks: whether two installed "
        "names an edit apart are two real packages or one misspelling. "
        "Every other check is mechanical, and without this step a "
        "near-miss stands rather than being settled — this is the CI path.",
    )
    p_pkg.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="List every installed package that was checked",
    )
    p_pkg.add_argument("--config", help="Path to .vibe-sentinel.toml")

    # --- credentials ---
    p_cred = sub.add_parser(
        "credentials",
        help="Check for passwords and keys sitting at rest: the files whose "
        "purpose is holding them, and the ones hardcoded into source",
    )
    p_cred.add_argument("root", nargs="?", default=None, help="Project root")
    p_cred.add_argument("--policy", default=None, help="Path to credential-policy.toml")
    p_cred.add_argument(
        "--no-model",
        action="store_true",
        help="Stop after the pattern match. Lists the candidates without "
        "adjudicating any of them, and says so — this is the CI path.",
    )
    p_cred.add_argument(
        "--home",
        action="store_true",
        help="Also check the credential stores in your home directory — "
        "~/.aws/credentials, ~/.netrc, ~/.ssh keys and the rest. They were "
        "never in a repository, so no .gitignore was ever keeping them out "
        "of one, and an agent reads them with `cat` like anything else.",
    )
    p_cred.add_argument(
        "--print-rules",
        action="store_true",
        help="Print the active rule set — what is looked for, and what the "
        "model is asked about each",
    )
    p_cred.add_argument("--config", help="Path to .vibe-sentinel.toml")

    # --- safety ---
    p_safety = sub.add_parser(
        "safety",
        help="Recorded verdicts on the agent's commands, or try the gate on one",
    )
    p_safety.add_argument("root", nargs="?", default=None, help="Project root")
    p_safety.add_argument(
        "--check",
        default=None,
        metavar="COMMAND",
        help="Run triage and a review on one command, against this project's "
        "real recent history. Blocks nothing. The verdict is recorded like "
        "any other — marked 'check', so no listing counts it as something an "
        "agent ran — because a verdict reached while tuning is still a "
        "verdict. The way to tune the gate without waiting for an agent to "
        "do something drastic.",
    )
    p_safety.add_argument(
        "--match",
        default=None,
        metavar="REGEX",
        help="Try a candidate pattern against the commands already recorded: "
        "what it would escalate, what the current set already catches, and "
        "what share of the agent's work it would send to the model. Writing "
        "a pattern is otherwise done blind.",
    )
    p_safety.add_argument(
        "--print-dangers",
        action="store_true",
        dest="print_dangers",
        help="Print the active danger set — what is checked for, and the "
        "question each one asks. Built-ins layered with this project's own.",
    )
    p_safety.add_argument(
        "--show-prompt",
        action="store_true",
        dest="show_prompt",
        help="With --check, print exactly what the model is asked",
    )
    p_safety.add_argument(
        "--no-model",
        action="store_true",
        dest="no_model",
        help="With --check, stop after triage",
    )
    p_safety.add_argument(
        "--verdict",
        choices=["safe", "unclear", "unsafe", "unreviewed"],
        default=None,
        help="List only verdicts of this kind",
    )
    p_safety.add_argument("--session", default=None, help="Claude Code session id")
    p_safety.add_argument("--limit", type=int, default=50)
    p_safety.add_argument("--config", help="Path to .vibe-sentinel.toml")

    # --- backend ---
    p_backend = sub.add_parser(
        "backend",
        help="Start, stop, or check the model backend (any OpenAI-compatible server)",
    )
    backend_sub = p_backend.add_subparsers(dest="backend_action")

    p_bstatus = backend_sub.add_parser(
        "status", help="Check the endpoint and show what it serves"
    )
    p_bstatus.add_argument("--config", help="Path to .vibe-sentinel.toml")

    p_bstart = backend_sub.add_parser(
        "start", help="Run [llm] start_command and wait for the endpoint"
    )
    p_bstart.add_argument(
        "--no-wait",
        action="store_true",
        dest="no_wait",
        help="Return as soon as the command exits, without waiting for the "
        "endpoint to answer",
    )
    p_bstart.add_argument("--config", help="Path to .vibe-sentinel.toml")

    p_bstop = backend_sub.add_parser("stop", help="Run [llm] stop_command")
    p_bstop.add_argument("--config", help="Path to .vibe-sentinel.toml")

    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv

    # Fast path for the installed hook, which Claude Code invokes as
    # exactly `vibe-sentinel hook`, once per tool call. Building the
    # parser and importing the command handlers costs more than the work
    # the hook actually does, and none of it is needed to read one
    # payload from stdin. Any other argv falls through to the parser
    # below, so this can only ever be faster — never different.
    if arguments == ["hook"]:
        import json

        from vibe_sentinel.hook import guard

        _, decision = guard(sys.stdin.read())
        if decision is not None:
            # The only thing this ever writes to stdout, and only when
            # the safety gate is set to enforce and said no.
            print(json.dumps(decision))
        return 0

    from vibe_sentinel.cli import (
        auto_check,
        run_backend,
        run_backups,
        run_commands,
        run_db,
        run_history,
        run_credentials,
        run_licenses,
        run_hook,
        run_migrate,
        run_packages,
        run_parameters,
        run_safety,
        run_scan,
        run_trend,
    )

    parser = _build_parser()
    args = parser.parse_args(arguments)

    if not args.command:
        parser.print_help()
        return 0

    # The hook runs in whatever directory the agent is working in, and
    # a file sink would drop a logs/ tree there. It logs to stderr only,
    # which Claude Code already routes to its own debug log.
    if args.command == "hook":
        return run_hook(args)

    # Imported here, not at module scope: `load_config` builds a pydantic
    # model, and the hook path above returns before ever needing one. That
    # import alone is ~30 ms of a hook invocation that does not use it.
    from vibe_sentinel.config import load_config

    config_arg = getattr(args, "config", None)
    _setup_logging(load_config(Path(config_arg) if config_arg else None))

    # Every command that is not itself about the database gets a health
    # check first — at most one a day, whatever the command. It writes to
    # stderr and cannot change what happens next: see cli.auto_check.
    auto_check(args)

    if args.command == "scan":
        return run_scan(args)
    if args.command == "history":
        return run_history(args)
    if args.command == "trend":
        return run_trend(args)
    if args.command == "parameters":
        return run_parameters(args)
    if args.command == "migrate":
        return run_migrate(args)
    if args.command == "backups":
        return run_backups(args)
    if args.command == "db":
        return run_db(args)
    if args.command == "commands":
        return run_commands(args)
    if args.command == "licenses":
        return run_licenses(args)
    if args.command == "packages":
        return run_packages(args)
    if args.command == "credentials":
        return run_credentials(args)
    if args.command == "safety":
        return run_safety(args)
    if args.command == "backend":
        return run_backend(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
