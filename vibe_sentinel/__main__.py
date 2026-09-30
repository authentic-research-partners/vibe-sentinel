"""CLI entry point: ``vibe-sentinel scan``

Surfaces:

- ``scan``       — run the probes, record the structure, report drift.
- ``history``    — list recorded runs, or show one in detail.
- ``trend``      — the recorded history, fitted: slopes and anomalies.
- ``parameters`` — the values a probe's placeholders were filled with.
- ``migrate``    — update the history database's schema.
- ``backups``    — list every copy of the database, however it was made.
- ``db``         — size, health, backup, prune, vacuum the history file.
- ``hook``       — record one agent tool call; install the hook.
- ``commands``   — what the coding agent ran, and which actor ran it.
- ``credentials``— passwords and keys sitting at rest in the tree.
- ``safety``     — verdicts on those commands, or try the gate on one.
- ``licenses``   — check dependency licences against the policy.
- ``packages``   — check dependency provenance: does it exist, who added it.
- ``backend``    — start, stop, or check the model server.

Command implementations live in :mod:`vibe_sentinel.cli`. This module is
the surface itself: one ``_add_*`` builder per command, listed once in
:data:`COMMANDS`, which is the order ``--help`` prints them in.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from vibe_sentinel.log import logger

from vibe_sentinel import __version__

if TYPE_CHECKING:
    from vibe_sentinel.config import SentinelConfig

    #: What ``add_subparsers()`` hands back. Named because every builder
    #: below takes one and the spelling is longer than the parameter it
    #: annotates. Type-checking only: ``_SubParsersAction`` is generic to
    #: the stubs and not subscriptable at runtime, and every annotation
    #: here is a string anyway (``from __future__ import annotations``).
    Subparsers = argparse._SubParsersAction[argparse.ArgumentParser]


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


def _command(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    help: str,
) -> argparse.ArgumentParser:
    """Register one subcommand, with its one-line help used twice.

    argparse keeps ``help`` and ``description`` apart and fills in neither
    from the other: ``help`` is the line the parent listing shows, and
    ``description`` is the sentence ``vibe-sentinel <name> --help`` prints
    above the flags. A command declared with only ``help`` therefore has a
    sub-help that names every option and never says what the command is
    for — which is what happened to all fourteen of them. Declaring a
    subcommand any other way is what ``test_cli_help.py`` refuses.
    """
    return sub.add_parser(name, help=help, description=help)


def _add_scan(sub: Subparsers) -> None:
    p_scan = _command(
        sub, "scan", help="Run the probes, record the structure, report drift"
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
        "--list-probes",
        action="store_true",
        help="Print the active probe set — the built-ins, whatever this "
        "project declares on top, the values each will run with, and which "
        "file every declared one came from. Then exit without scanning.",
    )
    p_scan.add_argument(
        "--list-lenses",
        action="store_true",
        help="Print the active lens set — what each question asks, which "
        "probes it watches, and which file declared it. A lens is only put "
        "to the model when the probes it watches moved, so this is where to "
        "look when one is never answered. Then exit without scanning.",
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


def _add_history(sub: Subparsers) -> None:
    p_hist = _command(sub, "history", help="List recorded runs, or show one")
    p_hist.add_argument("root", nargs="?", default=None, help="Project root")
    p_hist.add_argument("--run", type=int, default=None, help="Show this run in detail")
    p_hist.add_argument("--limit", type=int, default=20, help="Runs to list")
    p_hist.add_argument(
        "-v", "--verbose", action="store_true", help="List every observation"
    )
    p_hist.add_argument("--config", help="Path to .vibe-sentinel.toml")


def _add_trend(sub: Subparsers) -> None:
    p_trend = _command(
        sub,
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


def _add_parameters(sub: Subparsers) -> None:
    p_params = _command(
        sub,
        "parameters",
        help="The values a probe's placeholders were filled with, per run",
    )
    p_params.add_argument("probe", help="Probe id")
    p_params.add_argument("root", nargs="?", default=None, help="Project root")
    p_params.add_argument(
        "--limit", type=int, default=20, help="Runs to list (default: 20)"
    )
    p_params.add_argument("--config", help="Path to .vibe-sentinel.toml")


def _add_migrate(sub: Subparsers) -> None:
    p_migrate = _command(
        sub, "migrate", help="Update the history database to this build's schema"
    )
    p_migrate.add_argument("root", nargs="?", default=None, help="Project root")
    p_migrate.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Show what would be applied without touching the database",
    )
    p_migrate.add_argument("--config", help="Path to .vibe-sentinel.toml")


def _add_backups(sub: Subparsers) -> None:
    p_backups = _command(
        sub, "backups", help="List database backups left by migrations"
    )
    p_backups.add_argument("root", nargs="?", default=None, help="Project root")
    p_backups.add_argument("--config", help="Path to .vibe-sentinel.toml")


def _add_db(sub: Subparsers) -> None:
    p_db = _command(
        sub,
        "db",
        help="Size, health, backup and retention for the history database",
    )
    db_sub = p_db.add_subparsers(dest="db_action")

    p_dbstatus = _command(
        db_sub, "status", help="What the database weighs, and when it was last checked"
    )
    p_dbstatus.add_argument("root", nargs="?", default=None, help="Project root")
    p_dbstatus.add_argument("--config", help="Path to .vibe-sentinel.toml")

    p_dbcheck = _command(
        db_sub,
        "check",
        help="Run the health check now and record it. Exit 1 when something "
        "needs attention.",
    )
    p_dbcheck.add_argument("root", nargs="?", default=None, help="Project root")
    p_dbcheck.add_argument(
        "--format",
        choices=["terminal", "json"],
        default=None,
        help="Output format (default: terminal)",
    )
    p_dbcheck.add_argument("--config", help="Path to .vibe-sentinel.toml")

    p_dbbackup = _command(
        db_sub,
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

    p_dbprune = _command(
        db_sub,
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

    p_dbvacuum = _command(
        db_sub,
        "vacuum",
        help="Rewrite the file compactly to reclaim deleted pages, then "
        "refresh the query planner's statistics. Backs up first.",
    )
    p_dbvacuum.add_argument("root", nargs="?", default=None, help="Project root")
    p_dbvacuum.add_argument("--config", help="Path to .vibe-sentinel.toml")

    p_dbreindex = _command(
        db_sub, "reindex", help="Recreate any declared index the database is missing"
    )
    p_dbreindex.add_argument("root", nargs="?", default=None, help="Project root")
    p_dbreindex.add_argument("--config", help="Path to .vibe-sentinel.toml")

    p_db.add_argument("--config", help="Path to .vibe-sentinel.toml")


def _add_hook(sub: Subparsers) -> None:
    p_hook = _command(
        sub,
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


def _add_commands(sub: Subparsers) -> None:
    p_commands = _command(
        sub,
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
    p_commands.add_argument(
        "--limit", type=int, default=50, help="Rows to list (default: 50)"
    )
    p_commands.add_argument(
        "--format",
        choices=["terminal", "json"],
        default=None,
        help="Output format (default: terminal)",
    )
    p_commands.add_argument("--config", help="Path to .vibe-sentinel.toml")


def _add_licenses(sub: Subparsers) -> None:
    p_lic = _command(
        sub,
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
        "-v", "--verbose", action="store_true", help="List every resolved package"
    )
    p_lic.add_argument("--config", help="Path to .vibe-sentinel.toml")


def _add_packages(sub: Subparsers) -> None:
    p_pkg = _command(
        sub,
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
        "-v",
        "--verbose",
        action="store_true",
        help="List every installed package that was checked",
    )
    p_pkg.add_argument("--config", help="Path to .vibe-sentinel.toml")


def _add_credentials(sub: Subparsers) -> None:
    p_cred = _command(
        sub,
        "credentials",
        help="Check for passwords and keys sitting at rest: the files whose "
        "purpose is holding them, and the ones hardcoded into source",
    )
    p_cred.add_argument("root", nargs="?", default=None, help="Project root")
    p_cred.add_argument("--policy", default=None, help="Path to credential-policy.toml")
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


def _add_safety(sub: Subparsers) -> None:
    p_safety = _command(
        sub,
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
        "--verdict",
        choices=["safe", "unclear", "unsafe", "unreviewed"],
        default=None,
        help="List only verdicts of this kind",
    )
    p_safety.add_argument("--session", default=None, help="Claude Code session id")
    p_safety.add_argument(
        "--limit", type=int, default=50, help="Verdicts to list (default: 50)"
    )
    p_safety.add_argument("--config", help="Path to .vibe-sentinel.toml")


def _add_backend(sub: Subparsers) -> None:
    p_backend = _command(
        sub,
        "backend",
        help="Start, stop, or check the model backend (any OpenAI-compatible server)",
    )
    backend_sub = p_backend.add_subparsers(dest="backend_action")

    p_bstatus = _command(
        backend_sub, "status", help="Check the endpoint and show what it serves"
    )
    p_bstatus.add_argument("--config", help="Path to .vibe-sentinel.toml")

    p_bstart = _command(
        backend_sub, "start", help="Run [llm] start_command and wait for the endpoint"
    )
    p_bstart.add_argument(
        "--no-wait",
        action="store_true",
        dest="no_wait",
        help="Return as soon as the command exits, without waiting for the "
        "endpoint to answer",
    )
    p_bstart.add_argument("--config", help="Path to .vibe-sentinel.toml")

    p_bstop = _command(backend_sub, "stop", help="Run [llm] stop_command")
    p_bstop.add_argument("--config", help="Path to .vibe-sentinel.toml")


#: Every subcommand, in the order ``--help`` prints them. A command
#: that is not here does not exist: there is one list, and adding to it
#: is how a command is registered.
COMMANDS: tuple[Callable[[Subparsers], None], ...] = (
    _add_scan,
    _add_history,
    _add_trend,
    _add_parameters,
    _add_migrate,
    _add_backups,
    _add_db,
    _add_hook,
    _add_commands,
    _add_licenses,
    _add_packages,
    _add_credentials,
    _add_safety,
    _add_backend,
)


def _build_parser() -> argparse.ArgumentParser:
    """The whole CLI surface, one builder per command.

    Split because ``--help`` is the interface and 500 lines in one
    function is where an option loses its ``help=`` unnoticed. The
    order of :data:`COMMANDS` is the order ``vibe-sentinel --help``
    lists them, so it is a decision rather than wherever a block
    happened to be pasted.
    """
    parser = argparse.ArgumentParser(
        prog="vibe-sentinel",
        description=(
            "Safeguards a codebase against the problems AI coding agents "
            "introduce: structural drift, secrets at rest, unvetted "
            "dependencies, and dangerous commands."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"vibe-sentinel {__version__}"
    )
    sub = parser.add_subparsers(dest="command")
    for add in COMMANDS:
        add(sub)
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

    from vibe_sentinel.cli import auto_check, run_hook

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
    from vibe_sentinel.cli import load_config_or_fail
    from vibe_sentinel.exceptions import ConfigError

    config_arg = getattr(args, "config", None)
    try:
        _setup_logging(load_config_or_fail(Path(config_arg) if config_arg else None))
    except ConfigError as e:
        # No file sink yet — `_setup_logging` is the call that just failed —
        # so this reaches loguru's default stderr handler and nothing else.
        # Still the logger rather than `print`: stdout is the result channel,
        # and an error is not a result.
        logger.error(str(e))
        return 2

    # Every command that is not itself about the database gets a health
    # check first — at most one a day, whatever the command. It writes to
    # stderr and cannot change what happens next: see cli.auto_check.
    auto_check(args)

    # One catch for every handler's own `_load`. A command reads the config
    # for the root it was pointed at, which is not the one read above — so
    # `vibe-sentinel scan /other/project` finds the broken file here, and it
    # is still a sentence naming the file rather than a traceback.
    try:
        return _dispatch(args, parser)
    except ConfigError as e:
        logger.error(str(e))
        return 2


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Route one parsed command to its handler."""
    from vibe_sentinel.cli import (
        run_backend,
        run_backups,
        run_commands,
        run_credentials,
        run_db,
        run_history,
        run_licenses,
        run_migrate,
        run_packages,
        run_parameters,
        run_safety,
        run_scan,
        run_trend,
    )

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
