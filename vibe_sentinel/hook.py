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
from vibe_sentinel.journal import HookEvent

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


def parse_event(raw: str) -> HookEvent | None:
    """Parse one hook payload, or None when it is not usable.

    Three things are unusable: text that is not JSON, JSON that is not an
    object, and an object with no ``tool_name``. Everything past that is
    coerced by :meth:`~vibe_sentinel.journal.HookEvent.from_payload` — a
    field of an unexpected type costs that field, not the event. Refusing
    the whole payload over one surprising value would drop a record of
    something the agent really did, at exactly the moment something
    unexpected was happening.

    ``tool_name`` is the exception because it is not a surprising field,
    it is the whole event: a ``PreToolUse`` payload always carries one,
    so an object without it came from a different program's schema. That
    distinction matters here more than most places. Coercion would happily
    accept it and append a row naming no tool and no command — a record
    asserting that an agent did something unidentifiable, which is worse
    than no row at all, and indistinguishable in the journal from a real
    call this build failed to read.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.debug("hook: payload is not JSON ({}) — ignoring", e)
        return None
    if not isinstance(payload, dict):
        logger.debug("hook: payload is {}, not an object — ignoring", type(payload))
        return None
    if not str(payload.get("tool_name") or "").strip():
        # Loud, not debug: every payload reaching here is one somebody
        # wired up, so this is a misconfiguration rather than noise, and
        # the fields it did carry are the only clue to whose schema it is.
        logger.warning(
            "hook: payload carries no 'tool_name', so nothing was recorded. "
            "This build reads Claude Code's PreToolUse schema; the payload "
            "had: {}",
            ", ".join(sorted(payload)[:10]) or "(no fields)",
        )
        return None
    return HookEvent.from_payload(payload)


def spill_path(root: Path) -> Path:
    """Where events go when the database is unavailable."""
    return db_path(root).parent / SPILL_NAME


def spill(root: Path, event: HookEvent, occurred_at: str, reason: str) -> bool:
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
                "payload": event.as_payload(),
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
    root: Path, event: HookEvent, occurred_at: str
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
            command_id = journal_store.record_command(conn, event, occurred_at)
    except (SchemaMismatchError, sqlite3.Error, OSError) as e:
        spill(root, event, occurred_at, str(e))
        return ("spilled", None)
    if command_id is None:
        return ("duplicate", None)
    return ("recorded", command_id)


def _deny(reason: str) -> dict[str, Any]:
    """The JSON that tells Claude Code to refuse this tool call."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _ask(reason: str) -> dict[str, Any]:
    """The JSON that hands this tool call to the person to approve or refuse.

    Claude Code shows its own permission prompt, with ``reason`` on it.
    Where nobody can answer — a ``claude -p`` run with no permission host —
    Claude Code denies the request instead, so ``reason`` has to carry the
    fix as well as the doubt.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }


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


def _unreadable_config(error: str) -> dict[str, Any]:
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
) -> dict[str, Any] | None:
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
    root: Path, event: HookEvent, command_id: int, occurred_at: str
) -> dict[str, Any] | None:
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
            signals = triage(
                event.tool_name, event.command_text(), event.target(), root, dangers
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
                event.session_id,
                event.agent_id,
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

            opinion = asyncio.run(
                review(
                    event.tool_name,
                    event.command_text(),
                    event.target(),
                    event.cwd,
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
            decision = "ask"
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
        _record_unreviewed(
            root,
            command_id,
            occurred_at,
            mode,
            f"the safety gate failed: {e}",
            "ask" if asking else "allow",
        )
        if asking:
            logger.warning("safety gate failed ({}) — asking about the command", e)
            return _ask(
                f"vibe-sentinel failed while judging this command ({e}), so it "
                f"has no verdict. Approve it only if you know what it does. The "
                f"failure is in the hook log; `vibe-sentinel safety --check "
                f"'<command>'` runs the same review where the error is visible."
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
    raw: str, *, default_cwd: Path | None = None
) -> tuple[str, dict[str, Any] | None]:
    """Record one payload, then decide whether to let the command run.

    Returns the outcome and, when the safety gate is set to ``enforce``
    and the command was judged unsafe, the JSON Claude Code should act
    on. The caller writes that to stdout; nothing else ever goes there.
    """
    event = parse_event(raw)
    if event is None:
        return ("unparsed", None)

    start = Path(event.cwd) if event.cwd else (default_cwd or Path.cwd())
    root = find_project_root(start)
    if root is None:
        logger.debug("hook: {} is not inside a watched project — ignoring", start)
        return ("unwatched", None)

    occurred_at = now_iso()
    outcome, command_id = record_event(root, event, occurred_at)
    if command_id is None:
        return (outcome, None)
    return (outcome, gate(root, event, command_id, occurred_at))


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


def _from_spill_line(line: str) -> tuple[HookEvent | None, str]:
    """One spill line back into an event and the time it happened."""
    try:
        record = json.loads(line)
        payload = record["payload"]
        occurred_at = str(record["occurred_at"])
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.debug("hook: unreadable spill line ({})", e)
        return (None, "")
    if not isinstance(payload, dict):
        return (None, "")
    return (HookEvent.from_payload(payload), occurred_at)


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


def is_installed(settings: dict[str, object], command: str = HOOK_COMMAND) -> bool:
    """Whether ``command`` is already wired to PreToolUse in ``settings``."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    entries = hooks.get("PreToolUse")
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for spec in entry.get("hooks", []):
            if isinstance(spec, dict) and spec.get("command") == command:
                return True
    return False


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
                f"`vibe-sentinel hook --install`."
            ) from e
        if not isinstance(loaded, dict):
            raise ValueError(
                f"{path} holds a {type(loaded).__name__}, not a JSON object. "
                f"Fix or move it, then re-run `vibe-sentinel hook --install`."
            )
        settings = loaded

    if is_installed(settings, command):
        return (path, False)

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(
            f"{path} has a 'hooks' key that is not an object. Fix it, then "
            f"re-run `vibe-sentinel hook --install`."
        )
    entries = hooks.setdefault("PreToolUse", [])
    if not isinstance(entries, list):
        raise ValueError(
            f"{path} has a 'hooks.PreToolUse' key that is not a list. Fix it, "
            f"then re-run `vibe-sentinel hook --install`."
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
