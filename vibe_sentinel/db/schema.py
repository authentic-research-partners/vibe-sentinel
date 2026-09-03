"""SQLite schema for the structural history database.

One database per project at ``<root>/.vibe-sentinel/history.db``. Every
scan appends a run; nothing is ever overwritten. That is deliberate — the
history *is* the product. A single snapshot can only answer "did anything
change since last time"; a history answers "this directory has gained two
modules a week for six weeks", which is the drift that actually matters
and the one no single comparison can see.

Because the history cannot be rebuilt from anything, schema changes are
versioned migrations rather than a `--fresh` rebuild.

Adding a migration:

1. Add a ``SCHEMA_V{N}`` SQL string below.
2. Add it to ``MIGRATIONS``.
3. Add verify queries to ``VERIFY`` for every table and column it changes.
4. Name every index it creates in ``EXPECT_INDEXES`` and every index it
   drops in ``ABSENT_INDEXES``. Indexes are declared, not queried — a
   ``VERIFY`` query cannot check one. See the note on those dicts.
5. Increment ``SCHEMA_VERSION``.
6. Regenerate the committed schema dump with ``sqlite3 <db> .schema``.
7. Commit the dump in the same commit as the migration.
"""

from __future__ import annotations

# Schema version — increment when adding a migration below.
SCHEMA_VERSION = 6


SCHEMA_V1 = """
-- Schema version tracking. One row per applied migration.
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per scan.
--
-- `is_baseline` marks the run that later scans compare against. Exactly
-- one run should carry it; `store.mark_baseline` clears the others in the
-- same transaction. Keeping the flag here rather than deriving "baseline
-- = most recent" is what lets a team accept drift deliberately: a scan
-- that finds drift does NOT become the new baseline unless asked.
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    root TEXT NOT NULL,
    model TEXT NOT NULL,
    used_model INTEGER NOT NULL,
    analyzed INTEGER NOT NULL DEFAULT 0,
    is_baseline INTEGER NOT NULL DEFAULT 0,
    probe_count INTEGER NOT NULL DEFAULT 0,
    observation_count INTEGER NOT NULL DEFAULT 0,
    change_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_baseline ON runs(is_baseline)
    WHERE is_baseline = 1;

-- One row per probe per run.
--
-- `parameters_json` is the point of this table: it records what the model
-- actually chose for each placeholder on this run. A model that picks a
-- different SOURCE_ROOT between runs would manufacture drift out of
-- nothing, and without this column that failure is invisible.
CREATE TABLE IF NOT EXISTS probe_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    probe_id TEXT NOT NULL,
    title TEXT NOT NULL,
    command_json TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    ok INTEGER NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    UNIQUE (run_id, probe_id)
);

CREATE INDEX IF NOT EXISTS idx_probe_runs_run ON probe_runs(run_id);
CREATE INDEX IF NOT EXISTS idx_probe_runs_probe ON probe_runs(probe_id);

-- One row per structural fact per run.
--
-- (probe_id, key) is the identity that makes a fact comparable across
-- runs; the index below serves the trend query, which reads one key's
-- values ordered by run. `value` is nullable because an observation may
-- record only that something exists.
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    probe_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value REAL,
    label TEXT NOT NULL DEFAULT '',
    attrs_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_observations_run ON observations(run_id);
CREATE INDEX IF NOT EXISTS idx_observations_trend
    ON observations(probe_id, key, run_id);

-- One row per change detected on a run, against `baseline_run_id`.
--
-- Stored rather than recomputed so a past verdict stays readable exactly
-- as it was reported, including the model's severity and note. Severity
-- is recomputed differently by a later model version; the record of what
-- was actually said at the time should not move.
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    baseline_run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    probe_id TEXT NOT NULL,
    key TEXT NOT NULL,
    kind TEXT NOT NULL,
    before_value REAL,
    after_value REAL,
    label TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_changes_run ON changes(run_id);
CREATE INDEX IF NOT EXISTS idx_changes_key ON changes(probe_id, key);
"""


SCHEMA_V2 = """
-- What the coding agent ran, recorded by the PreToolUse hook.
--
-- One row per *actor*, not per session: a session and each subagent
-- working under it get their own row, keyed by (session_id, agent_id).
-- The main thread carries an empty agent_id, so a subagent's parent is
-- the row sharing its session_id with agent_id = ''. Collapsing them
-- into one row per session would interleave two parallel subagents into
-- a single stream that reads as one confused actor.
--
-- command_count is maintained by the writer rather than counted on
-- demand: listing actors is the common read, and every row of this table
-- is written from a hook the agent is waiting on.
CREATE TABLE IF NOT EXISTS agent_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    cwd TEXT NOT NULL,
    transcript_path TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    command_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (session_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_sessions_session
    ON agent_sessions(session_id);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_seen
    ON agent_sessions(last_seen_at);

-- One row per tool call, as the hook saw it *before* it ran.
--
-- `command` holds the shell command for the tools that run one; `target`
-- holds the path, pattern or URL for the ones that don't. Neither is the
-- whole of tool_input, deliberately: a Write's arguments carry the file's
-- entire new content, and this table records what the agent did, not a
-- second copy of the codebase. `envelope_json` is everything except those
-- arguments, so a payload that gains a field keeps it without a
-- migration.
--
-- occurred_at is when the hook fired, in milliseconds — the payload
-- carries no timestamp of its own, and dozens of calls can land inside
-- one second. It compares correctly against runs.started_at even though
-- that is seconds: where the two differ, '.' sorts after '+'.
CREATE TABLE IF NOT EXISTS agent_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_session_id INTEGER NOT NULL
        REFERENCES agent_sessions(id) ON DELETE CASCADE,
    occurred_at TEXT NOT NULL,
    prompt_id TEXT NOT NULL,
    tool_use_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    command TEXT NOT NULL,
    target TEXT NOT NULL,
    description TEXT NOT NULL,
    cwd TEXT NOT NULL,
    permission_mode TEXT NOT NULL,
    envelope_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_commands_session
    ON agent_commands(agent_session_id, id);
CREATE INDEX IF NOT EXISTS idx_agent_commands_time
    ON agent_commands(occurred_at);
CREATE INDEX IF NOT EXISTS idx_agent_commands_tool
    ON agent_commands(tool_name);
CREATE INDEX IF NOT EXISTS idx_agent_commands_prompt
    ON agent_commands(prompt_id) WHERE prompt_id != '';

-- Idempotency. A spilled event replayed after a migration, or a hook
-- that fires twice for one call, must not become two rows: a duplicate
-- would silently inflate every count taken from this table. Partial,
-- because an absent tool_use_id is empty rather than null and several
-- rows may legitimately carry ''.
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_commands_use_id
    ON agent_commands(tool_use_id) WHERE tool_use_id != '';
"""


SCHEMA_V3 = """
-- What a probe measured about an observation's provenance.
--
-- A column rather than another key in attrs_json, for one reason: this is the
-- field you query the history BY. "Which runs carried an unregistered import",
-- "when did this package stop being an orphan" are questions about a whole
-- history, and answering them by unpacking JSON in every row makes the index
-- impossible. Everything else a probe wants to attach stays in attrs_json.
--
-- The vocabulary is the probe's, not this schema's — a short mechanical label
-- like 'orphan' or 'squatted', set deterministically by the probe that measured
-- it. It is deliberately NOT a severity: severity is the model's word for how
-- much a change matters and lives on `changes`, while this is a fact about the
-- observation and the model never writes it.
--
-- Empty for observations with nothing to record, which is most of them; the
-- index is partial so those rows cost nothing.
ALTER TABLE observations ADD COLUMN risk TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_observations_risk
    ON observations(risk, probe_id, run_id) WHERE risk != '';
"""


SCHEMA_V4 = """
-- One row per command that was triaged as worth a second opinion.
--
-- Separate from `agent_commands` on purpose, and for the same reason
-- `changes` is separate from `observations`: the record of what an agent
-- ran must not depend on anyone's opinion of it. A later model version
-- rating the same command differently adds a row here; it does not
-- rewrite what happened.
--
-- `reviewed` is the one that matters for honesty. 0 means triage flagged
-- the command and the model never answered — backend down, timed out,
-- or disabled — so the verdict is mechanical and nothing may report it
-- as a review. Same rule as `runs.analyzed`.
--
-- `history_count` records how much of the actor's own past the model was
-- shown. A verdict on `rm -rf $TARGET` reached with no history is a
-- different claim from the same verdict reached with a hundred commands
-- of context, and the difference should survive in the record.
CREATE TABLE IF NOT EXISTS command_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id INTEGER NOT NULL
        REFERENCES agent_commands(id) ON DELETE CASCADE,
    reviewed_at TEXT NOT NULL,
    signals TEXT NOT NULL,
    verdict TEXT NOT NULL,
    reason TEXT NOT NULL,
    model TEXT NOT NULL,
    reviewed INTEGER NOT NULL,
    mode TEXT NOT NULL,
    enforced INTEGER NOT NULL,
    history_count INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    UNIQUE (command_id)
);

CREATE INDEX IF NOT EXISTS idx_command_reviews_verdict
    ON command_reviews(verdict);
CREATE INDEX IF NOT EXISTS idx_command_reviews_time
    ON command_reviews(reviewed_at);
"""


SCHEMA_V5 = """
-- An index audit, and the record of the database's own upkeep.
--
-- Four of the indexes below replace one that was a prefix of them. A
-- prefix index answers nothing the longer one doesn't, and every one of
-- them is a second B-tree the writer maintains — on `agent_commands`
-- that writer is a PreToolUse hook the agent is waiting on. So these are
-- DROP-then-CREATE, not additions.
--
-- The trailing column in each is the one the query sorts by. Without it
-- SQLite finds the rows by index and then builds a temp B-tree to order
-- them, which is invisible at a thousand rows and is the whole cost at a
-- million.

-- `parameters --limit N` reads one probe's rows newest-run first.
CREATE INDEX IF NOT EXISTS idx_probe_runs_probe_run
    ON probe_runs(probe_id, run_id);

-- `risks_at` asks one run for its flagged observations. The existing
-- risk index leads with `risk`, which cannot answer "run N, any risk";
-- this one leads with the run. Partial for the same reason as that one:
-- most observations carry no risk and should cost nothing here.
CREATE INDEX IF NOT EXISTS idx_observations_run_risk
    ON observations(run_id, risk) WHERE risk != '';

-- `changes --probe P --key K` reads one key's history newest run first.
DROP INDEX IF EXISTS idx_changes_key;
CREATE INDEX IF NOT EXISTS idx_changes_key_run
    ON changes(probe_id, key, run_id);

-- `commands --agent-type Explore` filtered nothing before this: with no
-- index on agent_type the planner scanned every row of agent_commands to
-- find the handful a subagent ran. The index goes on agent_sessions,
-- which is written once per actor, never on the per-tool-call table.
CREATE INDEX IF NOT EXISTS idx_agent_sessions_type
    ON agent_sessions(agent_type, id);

-- `commands --tool Bash`, newest first.
DROP INDEX IF EXISTS idx_agent_commands_tool;
CREATE INDEX IF NOT EXISTS idx_agent_commands_tool_id
    ON agent_commands(tool_name, id);

-- `safety --verdict unsafe`, newest first.
DROP INDEX IF EXISTS idx_command_reviews_verdict;
CREATE INDEX IF NOT EXISTS idx_command_reviews_verdict_id
    ON command_reviews(verdict, id);

-- One row per act of upkeep: a health check, a backup, a prune, a
-- vacuum. Two jobs, and both need it to be a table rather than a
-- timestamp file.
--
-- The automatic check reads the newest `health` row to decide whether a
-- day has passed, and a stamp file would answer that too. But "when was
-- this database last backed up" and "what did the last check complain
-- about" are questions about the database, asked of the database, and a
-- file beside it can be deleted, copied, or left behind by a restore
-- while the database it describes moves on. Here it travels with the
-- file, including into a backup.
--
-- `ok` is the honesty column, the same one `runs.analyzed` and
-- `command_reviews.reviewed` are: 0 means the upkeep was attempted and
-- did not complete. A prune that failed halfway must not read later as a
-- prune that found nothing to do.
--
-- `findings_json` holds what needed attention, as reported at the time.
-- Stored rather than recomputed for the same reason `changes` is: a
-- later build with a different threshold would silently rewrite the
-- history of what this database was complaining about.
CREATE TABLE IF NOT EXISTS db_maintenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    performed_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    ok INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    wal_bytes INTEGER NOT NULL,
    free_bytes INTEGER NOT NULL,
    findings_json TEXT NOT NULL,
    detail TEXT NOT NULL,
    duration_ms INTEGER NOT NULL
);

-- The automatic check's only read: the newest row of one kind.
CREATE INDEX IF NOT EXISTS idx_db_maintenance_kind
    ON db_maintenance(kind, performed_at);
"""


SCHEMA_V6 = """
-- What the deterministic gates found to be true of the tree, per run.
--
-- Separate from `observations` and `changes` because a gate finding is
-- not drift and must not be compared like it. `compare()` reports a key
-- when it appears and says nothing while it stays: correct for "this
-- directory gained a module", wrong for "there is a live key in this
-- repository", which is as true on the two-hundredth scan as the first.
-- Licences, provenance and credentials ran as probes for a while, and a
-- `.env` present on the first scan went into the baseline and was never
-- mentioned again. These tables exist so a standing fact is recorded as
-- a standing fact.
--
-- `run_id` is NULL when the gate was run on its own — `vibe-sentinel
-- credentials` rather than `vibe-sentinel scan`. Both are recorded: the
-- gate commands kept no history at all before this, so the one answer
-- that cost a model call was the one answer thrown away.
--
-- `adjudicated` is the honesty column, the same one `runs.analyzed` and
-- `command_reviews.reviewed` are. 0 means no model rated these findings
-- and every verdict below is mechanical; nothing may render them as a
-- review. `ok` 0 means the gate did not complete, which is different
-- from a gate that completed and found nothing.
--
-- `configured` 0 is the third state, and it is not a failure: this
-- project never declared a policy for this gate. Kept apart from `ok`
-- because the two need opposite handling -- an unconfigured gate must
-- not fail a scan, and must not be rendered as clean either.
CREATE TABLE IF NOT EXISTS gate_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    gate TEXT NOT NULL,
    started_at TEXT NOT NULL,
    root TEXT NOT NULL,
    ok INTEGER NOT NULL,
    configured INTEGER NOT NULL DEFAULT 1,
    adjudicated INTEGER NOT NULL DEFAULT 0,
    finding_count INTEGER NOT NULL DEFAULT 0,
    failing_count INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0
);

-- `gates --gate credentials`, newest first.
CREATE INDEX IF NOT EXISTS idx_gate_runs_gate ON gate_runs(gate, id);
-- One scan's gate results, for the report that follows it.
CREATE INDEX IF NOT EXISTS idx_gate_runs_run ON gate_runs(run_id);

-- One row per finding, keyed so its history is a query.
--
-- `key` is stable across runs for the same fact -- `<rule>:<path>` for a
-- credential, `<kind>:<name>` for a provenance finding, the package name
-- for a licence -- which is what makes "when did this start" answerable
-- without the finding itself being a diff.
--
-- `detail` holds the REDACTED excerpt and nothing else. `credentials.py`
-- keeps two, and its own docstring names `excerpt` as the only one that
-- may be printed, logged, or written here; the value never reaches this
-- table, because a history that cannot be rebuilt is the last place a
-- live key should be copied to.
--
-- `pinned` records that a policy already settled this one. It is kept
-- rather than dropped: a pin is a decision somebody made on a date, and
-- the run where a finding stopped failing because a pin arrived is worth
-- as much as the run where it started.
CREATE TABLE IF NOT EXISTS gate_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gate_run_id INTEGER NOT NULL REFERENCES gate_runs(id) ON DELETE CASCADE,
    gate TEXT NOT NULL,
    key TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    risk TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL DEFAULT '',
    failing INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    adjudicated INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    attrs_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_gate_findings_run ON gate_findings(gate_run_id);
-- "when did this start" -- one key's history, newest gate run first.
CREATE INDEX IF NOT EXISTS idx_gate_findings_key
    ON gate_findings(gate, key, gate_run_id);
"""


MIGRATIONS: dict[int, str] = {
    1: SCHEMA_V1,
    2: SCHEMA_V2,
    3: SCHEMA_V3,
    4: SCHEMA_V4,
    5: SCHEMA_V5,
    6: SCHEMA_V6,
}


#: Queries proving each migration landed. A ``SELECT ... LIMIT 0`` fails
#: if the table or any named column is missing, without reading rows.
VERIFY: dict[int, list[str]] = {
    1: [
        "SELECT version, applied_at FROM schema_version LIMIT 0",
        "SELECT id, started_at, root, model, used_model, analyzed,"
        " is_baseline, probe_count, observation_count, change_count"
        " FROM runs LIMIT 0",
        "SELECT id, run_id, probe_id, title, command_json, parameters_json,"
        " ok, error, summary, duration_ms FROM probe_runs LIMIT 0",
        "SELECT id, run_id, probe_id, key, value, label, attrs_json"
        " FROM observations LIMIT 0",
        "SELECT id, run_id, baseline_run_id, probe_id, key, kind, before_value,"
        " after_value, label, severity, note FROM changes LIMIT 0",
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_runs_started'",
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_runs_baseline'",
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_probe_runs_run'",
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_probe_runs_probe'",
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_observations_run'",
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_observations_trend'",
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_changes_run'",
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_changes_key'",
    ],
    2: [
        "SELECT id, session_id, agent_id, agent_type, cwd, transcript_path,"
        " first_seen_at, last_seen_at, command_count FROM agent_sessions LIMIT 0",
        "SELECT id, agent_session_id, occurred_at, prompt_id, tool_use_id,"
        " tool_name, command, target, description, cwd, permission_mode,"
        " envelope_json FROM agent_commands LIMIT 0",
        "SELECT 1 FROM sqlite_master WHERE type='index'"
        " AND name='idx_agent_sessions_session'",
        "SELECT 1 FROM sqlite_master WHERE type='index'"
        " AND name='idx_agent_sessions_seen'",
        "SELECT 1 FROM sqlite_master WHERE type='index'"
        " AND name='idx_agent_commands_session'",
        "SELECT 1 FROM sqlite_master WHERE type='index'"
        " AND name='idx_agent_commands_time'",
        "SELECT 1 FROM sqlite_master WHERE type='index'"
        " AND name='idx_agent_commands_tool'",
        "SELECT 1 FROM sqlite_master WHERE type='index'"
        " AND name='idx_agent_commands_prompt'",
        "SELECT 1 FROM sqlite_master WHERE type='index'"
        " AND name='idx_agent_commands_use_id'",
        "SELECT 1 FROM sqlite_master WHERE type='index'"
        " AND name='idx_agent_commands_use_id' AND sql LIKE '%WHERE%'",
    ],
    3: [
        "SELECT id, run_id, probe_id, key, value, label, attrs_json, risk"
        " FROM observations LIMIT 0",
        "SELECT 1 FROM sqlite_master WHERE type='index'"
        " AND name='idx_observations_risk'",
        "SELECT 1 FROM sqlite_master WHERE type='index'"
        " AND name='idx_observations_risk' AND sql LIKE '%WHERE%'",
    ],
    4: [
        "SELECT id, command_id, reviewed_at, signals, verdict, reason, model,"
        " reviewed, mode, enforced, history_count, duration_ms"
        " FROM command_reviews LIMIT 0",
        "SELECT 1 FROM sqlite_master WHERE type='index'"
        " AND name='idx_command_reviews_verdict'",
        "SELECT 1 FROM sqlite_master WHERE type='index'"
        " AND name='idx_command_reviews_time'",
    ],
    # Only the new table's columns. This migration's real subject is its
    # index set, and index names are declared in EXPECT_INDEXES /
    # ABSENT_INDEXES below rather than queried — see the note there for
    # why a query cannot check one.
    5: [
        "SELECT id, performed_at, kind, ok, size_bytes, wal_bytes, free_bytes,"
        " findings_json, detail, duration_ms FROM db_maintenance LIMIT 0",
    ],
    6: [
        "SELECT id, run_id, gate, started_at, root, ok, configured, adjudicated,"
        " finding_count, failing_count, summary, error, duration_ms"
        " FROM gate_runs LIMIT 0",
        "SELECT id, gate_run_id, gate, key, kind, subject, label, detail, risk,"
        " verdict, failing, pinned, adjudicated, reason, attrs_json"
        " FROM gate_findings LIMIT 0",
    ],
}


#: Indexes each migration must leave in place, checked against
#: ``sqlite_master`` after its ``VERIFY`` queries run.
#:
#: These are separate from ``VERIFY`` because a query cannot express them.
#: ``_verify_one`` proves a statement is *valid*, which is the right test for
#: "does this table have this column" — ``SELECT col FROM t LIMIT 0`` fails to
#: prepare when the column is missing. It is no test at all for an index:
#: ``SELECT 1 FROM sqlite_master WHERE name='idx_x'`` prepares perfectly
#: whether or not ``idx_x`` exists, and returning no rows is not an error. So
#: index names are declared, not queried.
EXPECT_INDEXES: dict[int, list[str]] = {
    1: [
        "idx_runs_started",
        "idx_runs_baseline",
        "idx_probe_runs_run",
        "idx_probe_runs_probe",
        "idx_observations_run",
        "idx_observations_trend",
        "idx_changes_run",
        "idx_changes_key",
    ],
    2: [
        "idx_agent_sessions_session",
        "idx_agent_sessions_seen",
        "idx_agent_commands_session",
        "idx_agent_commands_time",
        "idx_agent_commands_tool",
        "idx_agent_commands_prompt",
        "idx_agent_commands_use_id",
    ],
    3: ["idx_observations_risk"],
    4: ["idx_command_reviews_verdict", "idx_command_reviews_time"],
    5: [
        "idx_probe_runs_probe_run",
        "idx_observations_run_risk",
        "idx_changes_key_run",
        "idx_agent_sessions_type",
        "idx_agent_commands_tool_id",
        "idx_command_reviews_verdict_id",
        "idx_db_maintenance_kind",
    ],
    6: [
        "idx_gate_runs_gate",
        "idx_gate_runs_run",
        "idx_gate_findings_run",
        "idx_gate_findings_key",
    ],
}

#: Indexes each migration must have removed. A migration that creates the
#: longer index and leaves its prefix behind passes every other check and
#: still costs the writer exactly what the drop was meant to save.
ABSENT_INDEXES: dict[int, list[str]] = {
    5: [
        "idx_changes_key",
        "idx_agent_commands_tool",
        "idx_command_reviews_verdict",
    ],
}


def head_indexes() -> frozenset[str]:
    """Every index a database at :data:`SCHEMA_VERSION` should carry.

    Derived rather than listed, so an index cannot be declared in one place
    and forgotten in the other. The health check reads this to notice an
    index that was dropped by hand — a silent one-line change to how every
    query on that table performs.
    """
    names: set[str] = set()
    for version in sorted(MIGRATIONS):
        names.update(EXPECT_INDEXES.get(version, []))
        names.difference_update(ABSENT_INDEXES.get(version, []))
    return frozenset(names)


#: Migrations that rebuild a table other tables reference by foreign key.
#: SQLite enforces FKs during DROP TABLE and would cascade deletes into
#: child rows, so the engine disables them around these and runs
#: ``PRAGMA foreign_key_check`` afterwards. Empty until such a migration
#: exists.
FK_OFF_MIGRATIONS: frozenset[int] = frozenset()
