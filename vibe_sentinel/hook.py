"""Recording what the coding agent ran, while it runs it.

Claude Code fires a ``PreToolUse`` hook before every tool call. This
module is that hook's other end: it reads the event from stdin and
appends one row to the project's history database.

It always records, and judges only when a project asks it to. Under
``[safety] mode = "off"``, the default, the hook writes nothing to stdout
and cannot deny a tool call, rewrite one, or stall the agent with an
opinion. ``observe`` also asks :mod:`vibe_sentinel.safety` for a verdict
and records it, still refusing nothing. ``enforce`` turns an ``unsafe``
verdict — or a policy the gate cannot load — into a ``deny`` decision on
stdout, the only thing ever written there. It exits 0 in every mode.
Recording is this module's job and judging is ``safety.py``'s, kept apart
for the reason the rest of vibe-sentinel keeps measuring a codebase apart
from evaluating it. A probe measures the shape the work left behind; this
measures the work itself.

**Identity is the point.** The payload carries enough to keep several
concurrent actors apart instead of collapsing them into one stream:

===================== =====================================================
``session_id``        the Claude Code session
``agent_id``          the subagent — empty string for the main thread
``agent_type``        the subagent's name (``Explore``, ``Plan``, …)
``prompt_id``         the turn: one user prompt
``tool_use_id``       this exact call; the idempotency key
===================== =====================================================

So ``(session_id, agent_id)`` names the actor, and a subagent's parent
is the row with the same ``session_id`` and an empty ``agent_id``. Two
subagents working in parallel keep separate, ordered histories.

**That contract is another program's, and this build has not seen every
part of it.** The field names come from Claude Code's hook reference
(``code.claude.com/docs/en/hooks``), read against Claude Code 2.1.258;
``agent_id`` and ``agent_type`` are documented as present only inside a
subagent, so a project whose agent has never spawned one will never see
them. Rather than trust that, every event is stored with its whole
envelope — including fields this build does not know about — and
``vibe-sentinel commands --fields`` reports which of them have actually
arrived. An assumption you can check against recorded data is a
different thing from one you cannot.

**A gap must never be silent.** An empty stretch in this log reads as
"the agent ran nothing", which is exactly the false statement the rest
of this tool is built to avoid. So when the database cannot be written —
a migration is pending, another process holds the write lock — the raw
event is appended to ``.vibe-sentinel/hook-spill.jsonl`` rather than
dropped, and ``vibe-sentinel hook --replay`` drains it afterwards.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vibe_sentinel.log import logger

from vibe_sentinel.paths import CONFIG_FILENAME
from vibe_sentinel.requirements import ManifestUnreadable
from vibe_sentinel.safety import (
    configured_safety,
    declared_verdict,
    load_dangers,
    review,
    triage,
)
from vibe_sentinel.db import journal_store
from vibe_sentinel.db.connection import (
    DB_PATH,
    JOURNAL_DURABILITY,
    SchemaMismatchError,
    db_path,
    get_db,
)
from vibe_sentinel.journal import MAX_COMMAND_CHARS, ActionRequest, Decision

#: Events that could not reach the database land here, one JSON object
#: per line. ``vibe-sentinel hook --replay`` drains it.
SPILL_NAME = "hook-spill.jsonl"

#: Stop appending to the spill file past this size. A hook that fills the
#: disk because a migration was pending for a month is a worse failure
#: than a missing row.
MAX_SPILL_BYTES = 50_000_000

#: What the hook entry in ``.claude/settings.json`` invokes.
HOOK_COMMAND = "vibe-sentinel hook"

#: Seconds Claude Code waits for the hook. Bounded, so a locked database
#: cannot stall a tool call for the default ten minutes — but with room
#: above ``safety.timeout``, which is the bound that actually matters
#: when the gate is on.
#:
#: Keeping ``safety.timeout`` under this is a security property, not a
#: tidiness one. Claude Code's hooks reference: "A timed-out command,
#: http, or mcp_tool hook doesn't block the tool call. The call continues
#: through the normal permission flow, so don't count on a stalled hook to
#: act as a gate." So a verdict that runs past this limit is not asked
#: about or refused — the hook is cancelled and the call proceeds, which
#: is the one fail-open path ``[safety] no_verdict`` cannot close. The
#: gate gives up at ``safety.timeout`` and answers; this must never be
#: what cuts it off.
HOOK_TIMEOUT = 20


def now_iso() -> str:
    """Timestamp for one recorded event.

    Milliseconds, unlike ``runs.started_at`` which is seconds: dozens of
    tool calls can land in the same second and their order is worth
    keeping. The two still compare correctly as strings — at the point
    they differ, ``'.'`` sorts after ``'+'`` — which is what lets
    :func:`~vibe_sentinel.db.journal_store.commands_for_run` bound a
    window by run timestamps.
    """
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def find_project_root(start: Path) -> Path | None:
    """Walk up from ``start`` for a project vibe-sentinel is watching.

    A project counts as watched once it has a config file or a history
    database. Returning None for everywhere else is deliberate: the hook
    may be installed in the user's global Claude Code settings, and it
    must not scatter ``.vibe-sentinel/`` directories through every repo
    the agent happens to open.
    """
    current = start.resolve()
    while True:
        if (current / CONFIG_FILENAME).is_file() or (current / DB_PATH).is_file():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def spill_path(root: Path) -> Path:
    """Where events go when the database is unavailable."""
    return db_path(root).parent / SPILL_NAME


def spill(root: Path, request: ActionRequest, occurred_at: str, reason: str) -> bool:
    """Append one unrecordable event to the spill file.

    Returns whether it landed. Failing here is the end of the line: the
    event is lost, logged, and the hook still exits 0.
    """
    path = spill_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > MAX_SPILL_BYTES:
            logger.warning(
                "hook: spill file {} is over {} bytes — dropping this event. "
                "Drain it with: vibe-sentinel hook --replay",
                path,
                MAX_SPILL_BYTES,
            )
            return False
        line = json.dumps(
            {
                "occurred_at": occurred_at,
                "reason": reason,
                "request": request.as_dict(),
            },
            sort_keys=True,
        )
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        logger.warning("hook: could not spill event to {} ({}) — event lost", path, e)
        return False
    logger.debug("hook: database unavailable ({}) — event spilled to {}", reason, path)
    return True


def record_event(
    root: Path, request: ActionRequest, occurred_at: str
) -> tuple[str, int | None]:
    """Record one event, spilling it if the database cannot take it.

    Returns the outcome — ``recorded``, ``duplicate`` (this
    ``tool_use_id`` was already stored, so this is a replay), or
    ``spilled`` — and the new row's id when there is one. The id is what
    a review attaches to; a spilled event has no history to be judged
    against and no row to hang a verdict on.
    """
    try:
        with get_db(root, JOURNAL_DURABILITY) as conn:
            command_id = journal_store.record_command(conn, request, occurred_at)
    except (SchemaMismatchError, sqlite3.Error, OSError) as e:
        spill(root, request, occurred_at, str(e))
        return ("spilled", None)
    if command_id is None:
        return ("duplicate", None)
    return ("recorded", command_id)


def _deny(reason: str) -> Decision:
    """Refuse this tool call."""
    return Decision("deny", reason)


def _ask(reason: str) -> Decision:
    """Hand this tool call to the person to approve or refuse.

    Where nobody can answer — a ``claude -p`` run with no permission host,
    or an agent event with no prompt — the call ends refused instead, so
    ``reason`` has to carry the fix as well as the doubt.
    """
    return Decision("ask", reason)


def _no_verdict_reason(
    verdict: str, reason: str, signals: tuple[str, ...], timeout: float
) -> str:
    """Why the gate is asking rather than deciding, and what fixes it."""
    flagged = ", ".join(signals)
    if verdict == "unreviewed":
        return (
            f"vibe-sentinel flagged this command ({flagged}) and got no verdict "
            f"within [safety] timeout ({timeout:g} s), so it cannot say whether "
            f"it is safe. Approve it only if you know what it does. Check the "
            f"model with `vibe-sentinel backend status` — a model the backend "
            f"had unloaded can miss the timeout while it loads."
        )
    return (
        f"vibe-sentinel flagged this command ({flagged}) and the model could "
        f"not decide whether it is safe: {reason} Approve it only if you know "
        f"what it does. To stop asking about a command you trust, declare a "
        f"verdict for its danger in .vibe-sentinel.toml, or set [safety] "
        f"no_verdict = 'allow'."
    )


def _unreadable_config(error: str) -> Decision:
    """What to do when the safety mode itself cannot be read.

    Refuse. The gate cannot establish whether it is switched on, so it
    cannot clear this command — and the alternative, treating an unreadable
    setting as ``off``, is how a config with a stray comma silently
    disabled enforcement while still saying ``enforce`` on the line above.

    Blunt on purpose: with the file unreadable every flagged command is
    refused, which is what makes it impossible to miss. The message carries
    the parse error and the command that shows it.
    """
    logger.warning("safety: {}", error)
    return _deny(
        f"vibe-sentinel could not read its own configuration, so it cannot "
        f"tell whether this command should be reviewed. {error}"
    )


def _record_unreviewed(
    root: Path,
    command_id: int,
    occurred_at: str,
    mode: str,
    reason: str,
    decision: str,
) -> None:
    """Record that a command was acted on with no verdict, and why.

    The row is what ``vibe-sentinel safety --list`` reads, so a command the
    gate could not judge shows up there as ``unreviewed`` with the reason,
    rather than only in a log. A command that already has a row keeps it.
    """
    try:
        with get_db(root, JOURNAL_DURABILITY) as conn:
            journal_store.save_review(
                conn,
                command_id=command_id,
                reviewed_at=occurred_at,
                signals="",
                verdict="unreviewed",
                reason=reason,
                model="",
                reviewed=False,
                mode=mode,
                enforced=decision == "deny",
                history_count=0,
                duration_ms=0,
                decision=decision,
            )
    except (SchemaMismatchError, sqlite3.Error, OSError) as e:
        logger.warning("safety: could not record the unreviewed command ({})", e)


def _untriageable(
    root: Path,
    command_id: int,
    occurred_at: str,
    mode: str,
    reason: str,
    fix: str,
) -> Decision | None:
    """What to do when something triage reads will not load.

    Two things can: the project's ``[[danger]]`` set, and its
    ``pyproject.toml``, which ``undeclared-install`` reads to know what is
    declared. Neither gets a substitute. The built-in set in place of a
    declared one produces a verdict about a policy nobody declared, and it
    reads exactly like a verdict about theirs — a shipped list cannot know
    which of your database hosts is production. "No manifest" in place of an
    unreadable one concludes that nothing is declared, so no install can be
    undeclared.

    So the gate says what is true: it could not triage, and therefore cannot
    clear the command. ``enforce`` refuses and names the fix. ``observe``
    never refuses — that is the mode's own meaning rather than a second path
    — and records that nothing was judged, and why, so
    ``vibe-sentinel safety --list`` shows the gap instead of a clean run.
    """
    logger.warning("safety: {}", reason)
    _record_unreviewed(
        root,
        command_id,
        occurred_at,
        mode,
        reason,
        "deny" if mode == "enforce" else "allow",
    )
    if mode != "enforce":
        return None
    return _deny(
        f"vibe-sentinel refused this command because it could not tell whether "
        f"the command is dangerous: {reason} Fix the file and check it with "
        f"{fix}, or set safety.mode to 'observe' in .vibe-sentinel.toml to "
        f"record without acting."
    )


def gate(
    root: Path,
    request: ActionRequest,
    command_id: int,
    occurred_at: str,
    *,
    cwd: Path | None = None,
) -> Decision | None:
    """Judge one recorded command, and decide whether to stop it.

    Returns the decision JSON for Claude Code, or None to say nothing —
    which is what happens for every command triage does not flag and every
    configuration that is not ``enforce``.

    Under ``enforce`` the gate never degrades silently. ``unsafe`` is
    refused. A command with no verdict — the model answered ``unclear``,
    did not answer in time, or the gate itself failed — is handed to the
    person to approve (``[safety] no_verdict = "ask"``, the default), and
    where nobody can answer Claude Code turns that into a refusal carrying
    the fix. ``no_verdict = "allow"`` lets it run instead, chosen on
    purpose. ``observe`` records and never acts.

    **Nothing here may raise.** A bug must not become a blocked command:
    under ``enforce`` it becomes a question, and elsewhere the command
    proceeds.
    """
    working_directory = cwd or (Path(request.cwd) if request.cwd else root)
    mode = "off"
    no_verdict = "allow"
    try:
        # The mode first, and before the danger set, for two reasons. It is
        # the cheaper read, so the common case — a project that never turned
        # this on — pays one `tomllib.load` and stops. And a project with the
        # gate off must not be refused a command because a `[[danger]]` table
        # it does not use will not parse.
        try:
            mode, no_verdict = configured_safety(root)
        except ValueError as e:
            return _unreadable_config(str(e))
        if mode == "off":
            return None

        try:
            dangers = load_dangers(root)
        except ValueError as e:
            return _untriageable(
                root,
                command_id,
                occurred_at,
                mode,
                f"the project's [[danger]] set could not be loaded: {e}",
                "`vibe-sentinel safety --print-dangers`",
            )

        try:
            # One patch can touch several files. Check each path before
            # deduplicating signals, so a harmless first file cannot hide
            # a later edit to the gate itself.
            signals = tuple(
                dict.fromkeys(
                    signal
                    for target in (request.targets or (request.target,))
                    for signal in triage(
                        request.tool_name,
                        request.command,
                        target,
                        root,
                        dangers,
                        cwd=working_directory,
                    )
                )
            )
        except ManifestUnreadable as e:
            return _untriageable(
                root,
                command_id,
                occurred_at,
                mode,
                str(e),
                "`vibe-sentinel packages`",
            )
        if not signals:
            return None

        from vibe_sentinel.config import load_config

        config_path = root / CONFIG_FILENAME
        config = load_config(config_path if config_path.is_file() else None)

        with get_db(root, JOURNAL_DURABILITY) as conn:
            history = journal_store.recent_commands_for_actor(
                conn,
                request.session_id,
                request.agent_id,
                limit=config.safety_history,
                before_id=command_id,
            )

        # A verdict the project already declared is not put to the
        # model: it is faster, it is the same answer every time, and it
        # still holds when there is no backend running.
        settled = declared_verdict(signals, dangers)

        started = time.perf_counter()
        # The only event loop the hook opens, and only on the branch that
        # already decided to ask the model. `import asyncio` stays inside
        # it for the same reason every other import here is deferred: this
        # runs in front of every tool call, and the common path — no
        # danger matched — must pay for nothing it does not use.
        if settled:
            opinion = None
        else:
            import asyncio

            review_target = (
                "\n".join(request.targets) if request.targets else request.target
            )
            if len(review_target) > MAX_COMMAND_CHARS:
                raise ValueError(
                    "the action names too many paths for a bounded safety review; "
                    "split it into smaller actions"
                )
            opinion = asyncio.run(
                review(
                    request.tool_name,
                    request.command,
                    review_target,
                    str(working_directory),
                    root,
                    signals,
                    list(history),
                    config,
                    dangers,
                )
            )
        duration_ms = int((time.perf_counter() - started) * 1000)

        reviewed = opinion is not None
        if settled is not None:
            verdict, reason = settled
            resolves_to = ""
        else:
            verdict = opinion.verdict if opinion is not None else "unreviewed"
            reason = opinion.reason if opinion is not None else ""
            resolves_to = opinion.resolves_to if opinion is not None else ""
        if mode != "enforce":
            decision = "allow"
        elif verdict == "unsafe":
            decision = "deny"
        elif verdict in ("unclear", "unreviewed") and no_verdict == "ask":
            decision = "ask" if request.can_ask else "deny"
        else:
            decision = "allow"
        enforced = decision == "deny"

        with get_db(root, JOURNAL_DURABILITY) as conn:
            journal_store.save_review(
                conn,
                command_id=command_id,
                reviewed_at=occurred_at,
                signals=",".join(signals),
                verdict=verdict,
                reason=reason,
                model=config.llm_model if reviewed else "",
                reviewed=reviewed,
                mode=mode,
                enforced=enforced,
                history_count=len(history),
                duration_ms=duration_ms,
                decision=decision,
            )

        if decision == "ask":
            return _ask(
                _no_verdict_reason(verdict, reason, signals, config.safety_timeout)
            )
        if decision == "deny" and verdict in ("unclear", "unreviewed"):
            return _deny(
                _no_verdict_reason(verdict, reason, signals, config.safety_timeout)
                + " This agent cannot ask from its tool hook, so the call is refused."
            )
        if decision != "deny":
            return None

        detail = f" It resolves to: {resolves_to}." if resolves_to else ""
        return _deny(
            f"vibe-sentinel refused this command. {reason}{detail} "
            f"This was judged against the last {len(history)} command(s) this "
            f"agent ran. Change the command, or set safety.mode to 'observe' "
            f"in .vibe-sentinel.toml to record the verdict without acting on it."
        )
    except Exception as e:  # noqa: BLE001 - never block a tool call over a bug
        asking = mode == "enforce" and no_verdict == "ask"
        unanswered = ("ask" if request.can_ask else "deny") if asking else "allow"
        _record_unreviewed(
            root,
            command_id,
            occurred_at,
            mode,
            f"the safety gate failed: {e}",
            unanswered,
        )
        if asking:
            logger.warning("safety gate failed ({}) — asking about the command", e)
            return Decision(
                unanswered,
                f"vibe-sentinel failed while judging this command ({e}), so it "
                f"has no verdict. Approve it only if you know what it does. The "
                f"failure is in the hook log; `vibe-sentinel safety --check "
                f"'<command>'` runs the same review where the error is visible."
                + (
                    " This agent cannot ask from its tool hook, so the call is refused."
                    if not request.can_ask
                    else ""
                ),
            )
        logger.warning("safety gate failed ({}) — command allowed through", e)
        return None


def handle(raw: str, *, default_cwd: Path | None = None) -> str:
    """Take one raw hook payload from stdin to a recorded row.

    Returns what happened: ``recorded``, ``duplicate``, ``spilled``,
    ``unparsed`` (not a usable payload), or ``unwatched`` (the working
    directory is not inside a project vibe-sentinel watches).
    """
    return guard(raw, default_cwd=default_cwd)[0]


def guard(
    raw: str, *, agent: str = "claude-code", default_cwd: Path | None = None
) -> tuple[str, dict[str, Any] | None]:
    """Record one payload, then decide whether to let the call run.

    ``agent`` names the adapter that reads the payload and writes the reply.
    Returns the outcome and the JSON that agent should act on — None to
    print nothing. The caller writes it to stdout; nothing else ever goes
    there.
    """
    from vibe_sentinel.agents import adapter

    reader = adapter(agent)
    request = reader.read(raw)
    if request is None:
        return ("unparsed", reader.reply(None, None))

    start = Path(request.cwd) if request.cwd else (default_cwd or Path.cwd())
    root = find_project_root(start)
    if root is None:
        logger.debug("hook: {} is not inside a watched project — ignoring", start)
        return ("unwatched", reader.reply(None, request))

    occurred_at = now_iso()
    outcome, command_id = record_event(root, request, occurred_at)
    if command_id is None:
        return (outcome, reader.reply(None, request))
    return (
        outcome,
        reader.reply(gate(root, request, command_id, occurred_at, cwd=start), request),
    )


def replay_spill(root: Path) -> tuple[int, int]:
    """Drain the spill file into the database.

    Returns ``(recorded, failed)``. The file is renamed before being read
    so a hook firing mid-replay appends to a fresh one instead of having
    its event dropped; anything that still fails is written back.
    """
    path = spill_path(root)
    if not path.is_file():
        return (0, 0)

    working = path.with_name(f"{path.name}.replaying-{os.getpid()}")
    path.rename(working)

    recorded = 0
    unrecoverable: list[str] = []
    try:
        with get_db(root, JOURNAL_DURABILITY) as conn:
            for line in working.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event, occurred_at = _from_spill_line(line)
                if event is None:
                    unrecoverable.append(line)
                    continue
                try:
                    journal_store.record_command(conn, event, occurred_at)
                except sqlite3.Error as e:
                    logger.debug("hook: replay failed for one event ({})", e)
                    unrecoverable.append(line)
                    continue
                recorded += 1
    except (SchemaMismatchError, sqlite3.Error, OSError):
        # The database is still unavailable. Put the file back exactly as
        # it was rather than losing a queue we were asked to preserve.
        working.rename(path)
        raise

    if unrecoverable:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(unrecoverable) + "\n")
    working.unlink(missing_ok=True)
    return (recorded, len(unrecoverable))


def _from_spill_line(line: str) -> tuple[ActionRequest | None, str]:
    """One spill line back into a request and the time it happened.

    A line carries ``request``, the engine's form. A line written before
    agents had adapters carries ``payload``, which was always Claude Code's,
    and is read through that adapter: a queue somebody was promised would be
    replayed is replayed, whichever build wrote it.
    """
    try:
        record = json.loads(line)
        occurred_at = str(record["occurred_at"])
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.debug("hook: unreadable spill line ({})", e)
        return (None, "")
    if not isinstance(record, dict):
        return (None, "")
    if isinstance(record.get("request"), dict):
        return (ActionRequest.from_dict(record["request"]), occurred_at)
    if isinstance(record.get("payload"), dict):
        from vibe_sentinel.agents.claude_code import HookEvent

        return (HookEvent.from_payload(record["payload"]).request(), occurred_at)
    return (None, "")


# ---------------------------------------------------------------------------
# Installing the hook
# ---------------------------------------------------------------------------


#: Which tools the hook fires for. ``*`` is every tool call, which is
#: the complete record; ``Bash`` is shell commands only, which costs one
#: process per shell command instead of one per tool call. The default
#: records everything and lets the reader filter — a row you did not
#: record cannot be recovered later.
DEFAULT_MATCHER = "*"


def settings_entry(
    command: str = HOOK_COMMAND, matcher: str = DEFAULT_MATCHER
) -> dict[str, object]:
    """The ``PreToolUse`` entry that makes Claude Code call this hook."""
    return {
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT}],
    }


def settings_path(root: Path) -> Path:
    """The project-local Claude Code settings file."""
    return root / ".claude" / "settings.json"


def is_this_hook(command: object) -> bool:
    """Whether a settings entry's ``command`` runs this tool's hook.

    ``vibe-sentinel hook``, however it is reached — a full path, ``uv run``,
    ``uvx`` — and with nothing after it: ``vibe-sentinel hook --install`` in
    a settings file would not be a hook that records anything.
    """
    if not isinstance(command, str):
        return False
    words = command.split()
    return (
        len(words) >= 2
        and words[-1] == "hook"
        and Path(words[-2]).name == HOOK_COMMAND.split()[0]
    )


def hook_matchers(
    settings: dict[str, object], command: str = HOOK_COMMAND
) -> list[str]:
    """The matcher of every PreToolUse entry in ``settings`` that runs this
    hook, or ``command`` exactly — empty when none does."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    entries = hooks.get("PreToolUse")
    if not isinstance(entries, list):
        return []
    found: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        specs = entry.get("hooks", [])
        for spec in specs if isinstance(specs, list) else []:
            if isinstance(spec, dict) and (
                spec.get("command") == command or is_this_hook(spec.get("command"))
            ):
                found.append(str(entry.get("matcher", "")))
    return found


def is_installed(settings: dict[str, object], command: str = HOOK_COMMAND) -> bool:
    """Whether this hook is already wired to PreToolUse in ``settings``."""
    return bool(hook_matchers(settings, command))


def claude_settings_files(root: Path) -> list[tuple[str, Path]]:
    """``(scope, path)`` for each Claude Code settings file that can carry a
    hook for this project, broadest first.

    The managed file is this tool's own in ``managed-settings.d``, written
    by ``hook --install --managed``; a hook an organisation put in its own
    ``managed-settings.json`` is not read here.
    """
    from vibe_sentinel import paths

    return [
        ("managed", paths.claude_managed_hook_path()),
        ("user", Path.home() / ".claude" / "settings.json"),
        ("project", settings_path(root)),
        ("local", root / ".claude" / "settings.local.json"),
    ]


def read_settings(path: Path) -> dict[str, object] | None:
    """A settings file as an object; None when there is no file.

    Raises ``ValueError`` naming the file when it is there and is not a JSON
    object: a file that will not parse is not a file with no hook in it.
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        loaded = json.loads(text) if text.strip() else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"{path} could not be read as JSON ({e})") from e
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} holds a {type(loaded).__name__}, not a JSON object")
    return loaded


def install(
    root: Path,
    command: str = HOOK_COMMAND,
    matcher: str = DEFAULT_MATCHER,
) -> tuple[Path, bool]:
    """Add the PreToolUse hook to the project's Claude Code settings.

    Returns ``(path, changed)``; ``changed`` is False when the entry was
    already there. An existing settings file is backed up beside itself
    before it is rewritten, and every other key in it is preserved.
    """
    path = settings_path(root)
    settings: dict[str, object] = {}
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        try:
            loaded = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as e:
            raise ValueError(
                f"{path} is not valid JSON ({e}). Fix or move it, then re-run "
                f"`vibe-sentinel hook --install --mode <choice>`."
            ) from e
        if not isinstance(loaded, dict):
            raise ValueError(
                f"{path} holds a {type(loaded).__name__}, not a JSON object. "
                f"Fix or move it, then re-run `vibe-sentinel hook --install --mode <choice>`."
            )
        settings = loaded

    if is_installed(settings, command):
        return (path, False)

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(
            f"{path} has a 'hooks' key that is not an object. Fix it, then "
            f"re-run `vibe-sentinel hook --install --mode <choice>`."
        )
    entries = hooks.setdefault("PreToolUse", [])
    if not isinstance(entries, list):
        raise ValueError(
            f"{path} has a 'hooks.PreToolUse' key that is not a list. Fix it, "
            f"then re-run `vibe-sentinel hook --install --mode <choice>`."
        )
    entries.append(settings_entry(command, matcher))

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        logger.info("backed up {} to {}", path, backup)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return (path, True)


# ---------------------------------------------------------------------------
# Choosing the mode at install
# ---------------------------------------------------------------------------

#: What ``hook --install --mode`` offers, and the ``[safety] mode`` each
#: writes. ``journal`` is the hook with the gate off: every call recorded,
#: none judged.
INSTALL_MODES: dict[str, str] = {
    "journal": "off",
    "observe": "observe",
    "enforce": "enforce",
}

#: What each ``[safety] mode`` does, in the words ``status`` and
#: ``hook --install`` print.
MODE_MEANING: dict[str, str] = {
    "off": "records every tool call; judges none and blocks none",
    "observe": "judges each flagged call and records the verdict; blocks none",
    "enforce": "refuses an unsafe call, and asks you about one with no verdict",
}

_TABLE_RE = re.compile(r"^\s*\[")
_SAFETY_RE = re.compile(r"^\s*\[\s*safety\s*\]\s*(?:#.*)?$")
_MODE_RE = re.compile(r"^\s*mode\s*=")
_DOTTED_RE = re.compile(r"^\s*safety\s*[.=]")


def set_mode(root: Path, mode: str) -> Path:
    """Write ``[safety] mode`` into the project's config; see :func:`set_mode_at`."""
    return set_mode_at(root / CONFIG_FILENAME, mode)


def set_mode_at(path: Path, mode: str) -> Path:
    """Write ``[safety] mode`` into one file, and prove it took.

    A line edit rather than a rewrite, so every comment in the file stays
    where it is: the ``mode`` line inside ``[safety]`` is replaced, or added
    under the header, or a ``[safety]`` table is appended. The file is then
    read back with :func:`~vibe_sentinel.safety.declared_safety`, the reader
    the hook uses, and if it does not declare ``mode`` the original bytes
    are put back and this raises — a file this could not edit must not be
    left reading as some other mode. What it declares, not what is in
    force: a system policy may hold the mode higher, and that is not this
    file failing to say what it was told.

    ``OSError`` — no permission, most often, for the system policy — is
    raised before anything is written.
    """
    from vibe_sentinel.safety import declared_safety

    if mode not in INSTALL_MODES.values():
        raise ValueError(
            f"mode {mode!r} is not one of {sorted(INSTALL_MODES.values())}"
        )
    original = path.read_text(encoding="utf-8") if path.is_file() else None
    lines = (original or "").splitlines()
    line = f'mode = "{mode}"'

    header = next((i for i, text in enumerate(lines) if _SAFETY_RE.match(text)), None)
    if header is None:
        if any(_DOTTED_RE.match(text) for text in lines):
            raise ValueError(
                f"{path} declares safety as a dotted key or an inline table, "
                f'which this does not edit. Set `mode = "{mode}"` under '
                f"[safety] yourself, then check it with `vibe-sentinel status`."
            )
        if lines and lines[-1].strip():
            lines.append("")
        lines += ["[safety]", line]
    else:
        end = next(
            (i for i in range(header + 1, len(lines)) if _TABLE_RE.match(lines[i])),
            len(lines),
        )
        existing = next(
            (i for i in range(header + 1, end) if _MODE_RE.match(lines[i])), None
        )
        if existing is None:
            lines.insert(header + 1, line)
        else:
            lines[existing] = line

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        written, _ = declared_safety(path)
    except ValueError as e:
        written, problem = None, str(e)
    else:
        problem = f"it reads back as {written!r}"
    if written != mode:
        if original is None:
            path.unlink()
        else:
            path.write_text(original, encoding="utf-8")
        raise ValueError(
            f"Could not set [safety] mode in {path}: {problem}. The file is "
            f'unchanged. Set `mode = "{mode}"` under [safety] yourself.'
        )
    return path


def install_managed(matcher: str = DEFAULT_MATCHER) -> tuple[Path, str]:
    """The Claude Code hook in this tool's own managed-settings file.

    Returns ``(path, content)`` once written. ``OSError`` — no permission,
    as a rule, because only an administrator should be able to write there
    — is raised with nothing written, and the caller prints both for
    whoever has the rights.
    """
    from vibe_sentinel import paths

    path = paths.claude_managed_hook_path()
    content = json.dumps(
        {"hooks": {"PreToolUse": [settings_entry(matcher=matcher)]}}, indent=2
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + "\n", encoding="utf-8")
    return path, content
