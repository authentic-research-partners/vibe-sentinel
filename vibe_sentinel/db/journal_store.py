"""Reading and writing the agent command journal.

Two tables, both append-only like everything else here:

``agent_sessions``
    one row per *actor* — a ``(session_id, agent_id)`` pair. The main
    thread carries an empty ``agent_id``; each subagent gets its own row
    under the same ``session_id``, which is what makes a subagent's
    parent findable and keeps two parallel subagents from interleaving
    into one unreadable stream.

``agent_commands``
    one row per tool call, as the ``PreToolUse`` hook saw it, before it
    ran.

Like :mod:`vibe_sentinel.db.store`, every function takes an open
connection; the caller owns the ``get_db`` scope.
"""

from __future__ import annotations

import json
import sqlite3

from vibe_sentinel.journal import (
    AgentSessionRecord,
    CommandRecord,
    HookEvent,
    ReviewRecord,
)

# Identity columns come from the session row, so a command can be read
# without a second query. Every reader wants "who ran this".
_SELECT_COMMANDS = """
SELECT c.id, c.occurred_at, c.prompt_id, c.tool_use_id, c.tool_name,
       c.command, c.target, c.description, c.cwd, c.permission_mode,
       s.session_id, s.agent_id, s.agent_type
FROM agent_commands c
JOIN agent_sessions s ON s.id = c.agent_session_id
"""


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def record_command(
    conn: sqlite3.Connection,
    event: HookEvent,
    occurred_at: str,
) -> int | None:
    """Append one tool call, creating its actor row if this is the first.

    Returns the new command's id, or None when this ``tool_use_id`` was
    already recorded. That check is what makes the hook safe to replay: a
    spilled event drained later must not double-count, and a duplicate
    row would silently inflate every count taken from this table.

    One transaction, because a command row whose actor row is missing
    could not be attributed to anyone, and an actor row whose count does
    not match its commands is a history that disagrees with itself.
    """
    conn.execute("BEGIN")
    try:
        row = conn.execute(
            "SELECT id FROM agent_sessions WHERE session_id = ? AND agent_id = ?",
            (event.session_id, event.agent_id),
        ).fetchone()

        if row is None:
            cursor = conn.execute(
                "INSERT INTO agent_sessions (session_id, agent_id, agent_type, cwd,"
                " transcript_path, first_seen_at, last_seen_at, command_count)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    event.session_id,
                    event.agent_id,
                    event.agent_type,
                    event.cwd,
                    event.transcript_path,
                    occurred_at,
                    occurred_at,
                ),
            )
            session_row_id = int(cursor.lastrowid or 0)
        else:
            session_row_id = int(row["id"])
            # agent_type is absent from some payloads and present in
            # others for the same actor; keep the first name we learn
            # rather than letting a later blank erase it.
            conn.execute(
                "UPDATE agent_sessions SET last_seen_at = ?,"
                " agent_type = CASE WHEN agent_type = '' THEN ? ELSE agent_type END"
                " WHERE id = ?",
                (occurred_at, event.agent_type, session_row_id),
            )

        cursor = conn.execute(
            "INSERT INTO agent_commands (agent_session_id, occurred_at, prompt_id,"
            " tool_use_id, tool_name, command, target, description, cwd,"
            " permission_mode, envelope_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (
                session_row_id,
                occurred_at,
                event.prompt_id,
                event.tool_use_id,
                event.tool_name,
                event.command_text(),
                event.target(),
                event.description(),
                event.cwd,
                event.permission_mode,
                json.dumps(event.envelope(), sort_keys=True, default=str),
            ),
        )
        if cursor.rowcount == 0:
            conn.commit()
            return None

        command_id = int(cursor.lastrowid or 0)
        conn.execute(
            "UPDATE agent_sessions SET command_count = command_count + 1 WHERE id = ?",
            (session_row_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return command_id


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _to_command(row: sqlite3.Row) -> CommandRecord:
    return CommandRecord(
        id=row["id"],
        occurred_at=row["occurred_at"],
        session_id=row["session_id"],
        agent_id=row["agent_id"],
        agent_type=row["agent_type"],
        prompt_id=row["prompt_id"],
        tool_use_id=row["tool_use_id"],
        tool_name=row["tool_name"],
        command=row["command"],
        target=row["target"],
        description=row["description"],
        cwd=row["cwd"],
        permission_mode=row["permission_mode"],
    )


def _to_session(row: sqlite3.Row) -> AgentSessionRecord:
    return AgentSessionRecord(
        id=row["id"],
        session_id=row["session_id"],
        agent_id=row["agent_id"],
        agent_type=row["agent_type"],
        cwd=row["cwd"],
        transcript_path=row["transcript_path"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        command_count=row["command_count"],
    )


def list_agent_sessions(
    conn: sqlite3.Connection,
    limit: int = 20,
    session_id: str | None = None,
) -> list[AgentSessionRecord]:
    """Actors seen by the hook, most recently active first."""
    sql = "SELECT * FROM agent_sessions"
    params: list[object] = []
    if session_id is not None:
        sql += " WHERE session_id = ?"
        params.append(session_id)
    sql += " ORDER BY last_seen_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return [_to_session(r) for r in conn.execute(sql, params)]


def list_commands(
    conn: sqlite3.Connection,
    limit: int = 50,
    session_id: str | None = None,
    agent_id: str | None = None,
    agent_type: str | None = None,
    prompt_id: str | None = None,
    tool_name: str | None = None,
) -> list[CommandRecord]:
    """Recorded commands, newest first.

    Every filter is opt-in and None means "no filter" — note that
    ``agent_id=""`` is a real filter, not an absent one: it selects the
    main thread's own commands and excludes every subagent's.
    """
    clauses: list[str] = []
    params: list[object] = []
    for column, value in (
        ("s.session_id", session_id),
        ("s.agent_id", agent_id),
        ("s.agent_type", agent_type),
        ("c.prompt_id", prompt_id),
        ("c.tool_name", tool_name),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)

    sql = _SELECT_COMMANDS
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY c.id DESC LIMIT ?"
    params.append(limit)
    return [_to_command(r) for r in conn.execute(sql, params)]


def recent_commands_for_actor(
    conn: sqlite3.Connection,
    session_id: str,
    agent_id: str,
    limit: int = 100,
    before_id: int | None = None,
) -> list[CommandRecord]:
    """One actor's own recent commands, **oldest first**.

    Oldest first because this is read as a transcript, not a listing —
    it is what a safety review is given so it can resolve a variable
    back to the command that set it.

    Scoped to ``(session_id, agent_id)`` rather than to the session: a
    subagent's history is its own. Two subagents working in parallel
    interleave in wall-clock order, and showing one the other's commands
    would invite exactly the wrong inference about what a variable holds.
    """
    sql = _SELECT_COMMANDS + " WHERE s.session_id = ? AND s.agent_id = ?"
    params: list[object] = [session_id, agent_id]
    if before_id is not None:
        sql += " AND c.id < ?"
        params.append(before_id)
    sql += " ORDER BY c.id DESC LIMIT ?"
    params.append(limit)
    rows = [_to_command(r) for r in conn.execute(sql, params)]
    return list(reversed(rows))


def save_review(
    conn: sqlite3.Connection,
    command_id: int,
    reviewed_at: str,
    signals: str,
    verdict: str,
    reason: str,
    model: str,
    reviewed: bool,
    mode: str,
    enforced: bool,
    history_count: int,
    duration_ms: int,
) -> int | None:
    """Record one verdict. Returns None if this command already has one.

    A command gets one verdict — the one that was acted on. Re-running a
    review later produces a different opinion from a different model, and
    overwriting would erase what the gate actually did at the time.
    """
    conn.execute("BEGIN")
    try:
        cursor = conn.execute(
            "INSERT INTO command_reviews (command_id, reviewed_at, signals,"
            " verdict, reason, model, reviewed, mode, enforced, history_count,"
            " duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT DO NOTHING",
            (
                command_id,
                reviewed_at,
                signals,
                verdict,
                reason,
                model,
                int(reviewed),
                mode,
                int(enforced),
                history_count,
                duration_ms,
            ),
        )
        if cursor.rowcount == 0:
            conn.commit()
            return None
        review_id = int(cursor.lastrowid or 0)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return review_id


def list_reviews(
    conn: sqlite3.Connection,
    limit: int = 50,
    verdict: str | None = None,
    session_id: str | None = None,
) -> list[ReviewRecord]:
    """Recorded verdicts, newest first."""
    sql = """
        SELECT r.*, c.command, c.tool_name, s.session_id, s.agent_id, s.agent_type
        FROM command_reviews r
        JOIN agent_commands c ON c.id = r.command_id
        JOIN agent_sessions s ON s.id = c.agent_session_id
    """
    clauses: list[str] = []
    params: list[object] = []
    if verdict is not None:
        clauses.append("r.verdict = ?")
        params.append(verdict)
    if session_id is not None:
        clauses.append("s.session_id = ?")
        params.append(session_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY r.id DESC LIMIT ?"
    params.append(limit)
    return [
        ReviewRecord(
            id=row["id"],
            command_id=row["command_id"],
            reviewed_at=row["reviewed_at"],
            signals=row["signals"],
            verdict=row["verdict"],
            reason=row["reason"],
            model=row["model"],
            reviewed=bool(row["reviewed"]),
            mode=row["mode"],
            enforced=bool(row["enforced"]),
            history_count=row["history_count"],
            duration_ms=row["duration_ms"],
            command=row["command"],
            tool_name=row["tool_name"],
            session_id=row["session_id"],
            agent_id=row["agent_id"],
            agent_type=row["agent_type"],
        )
        for row in conn.execute(sql, params)
    ]


def commands_for_run(
    conn: sqlite3.Connection,
    run_id: int,
    limit: int = 500,
) -> list[CommandRecord]:
    """The commands recorded between the previous scan and this one.

    This is why the journal shares a database with the scans: a run says
    the structure moved, and this says what the agent was doing while it
    moved. Neither answers "why did this directory grow" on its own.

    The window is bounded by run timestamps, so it is a correlation, not
    a causal record — work done outside a hooked session leaves no rows
    here, and the boundary is only as precise as ``runs.started_at``.
    """
    row = conn.execute("SELECT started_at FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return []
    until = row["started_at"]

    previous = conn.execute(
        "SELECT started_at FROM runs WHERE id < ? ORDER BY id DESC LIMIT 1",
        (run_id,),
    ).fetchone()

    sql = _SELECT_COMMANDS + " WHERE c.occurred_at <= ?"
    params: list[object] = [until]
    if previous is not None:
        sql += " AND c.occurred_at > ?"
        params.append(previous["started_at"])
    sql += " ORDER BY c.id DESC LIMIT ?"
    params.append(limit)
    return [_to_command(r) for r in conn.execute(sql, params)]


def tool_counts(
    conn: sqlite3.Connection,
    session_id: str | None = None,
) -> list[tuple[str, int]]:
    """How many times each tool was called, most-used first.

    The join to ``agent_sessions`` appears only when a session is being
    filtered on. Unfiltered, this counts every row in the journal, and
    joining each one to its actor to discard the result is the whole cost
    of the query — a lookup per tool call, for a column nothing reads.
    """
    params: list[object] = []
    if session_id is None:
        sql = "SELECT tool_name, COUNT(*) AS n FROM agent_commands"
    else:
        sql = (
            "SELECT c.tool_name AS tool_name, COUNT(*) AS n FROM agent_commands c"
            " JOIN agent_sessions s ON s.id = c.agent_session_id"
            " WHERE s.session_id = ?"
        )
        params.append(session_id)
    sql += " GROUP BY tool_name ORDER BY n DESC, tool_name"
    return [(r["tool_name"], r["n"]) for r in conn.execute(sql, params)]


def _is_populated(value: object) -> bool:
    """Whether a payload field actually carried something.

    Spelled out rather than a falsiness test because ``0`` is a value and
    ``False`` is not one this payload ever means to send.
    """
    if value is None or value is False:
        return False
    if isinstance(value, (str, dict, list)):
        return bool(value)
    return True


def observed_fields(
    conn: sqlite3.Connection,
    limit: int = 5000,
) -> tuple[int, list[tuple[str, int]]]:
    """How often each payload field has arrived carrying a value.

    Returns ``(events_scanned, [(field, times_populated)])`` over the
    most recent events. This is why the envelope is stored: the field
    names the journal depends on come from Claude Code's documentation,
    and this turns them into something checkable against what it actually
    sends. A documented field that never arrives, or an undocumented one
    that does, shows up here first.

    Counts populated values, not present keys — the envelope always holds
    every declared field, so "was ``agent_id`` ever non-empty" is the
    question worth answering.
    """
    counts: dict[str, int] = {}
    scanned = 0
    for row in conn.execute(
        "SELECT envelope_json FROM agent_commands ORDER BY id DESC LIMIT ?",
        (limit,),
    ):
        scanned += 1
        try:
            envelope = json.loads(row["envelope_json"])
        except json.JSONDecodeError:
            continue
        if not isinstance(envelope, dict):
            continue
        for key, value in envelope.items():
            if _is_populated(value):
                counts[key] = counts.get(key, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return (scanned, ordered)


def journal_totals(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """``(sessions, actors, commands)`` currently recorded."""
    actors = conn.execute("SELECT COUNT(*) AS n FROM agent_sessions").fetchone()["n"]
    sessions = conn.execute(
        "SELECT COUNT(DISTINCT session_id) AS n FROM agent_sessions"
    ).fetchone()["n"]
    commands = conn.execute("SELECT COUNT(*) AS n FROM agent_commands").fetchone()["n"]
    return (int(sessions), int(actors), int(commands))
