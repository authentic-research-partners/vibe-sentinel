"""The migration engine: copy-on-write, verify, swap, backup.

These matter more than most tests here. The history database is the one
artifact vibe-sentinel cannot regenerate — if a migration corrupts it,
every past run it would have compared against is gone.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from vibe_sentinel.db import connection, journal_store, migration, schema
from vibe_sentinel.db.connection import (
    SchemaMismatchError,
    db_path,
    get_db,
    init_db,
    read_schema_version,
)
from vibe_sentinel.journal import HookEvent
from vibe_sentinel.db.migration import (
    BACKUP_DIR_NAME,
    MigrationError,
    cleanup_old_backups,
    get_status,
    list_backups,
    run_migration,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    init_db(db_path(tmp_path))
    return tmp_path


# --- status ----------------------------------------------------------------


def test_fresh_database_is_at_current_version(project: Path) -> None:
    status = get_status(project)
    assert status.current_version == schema.SCHEMA_VERSION
    assert status.up_to_date
    assert status.pending == []


def test_absent_database_reports_version_zero(tmp_path: Path) -> None:
    status = get_status(tmp_path)
    assert status.current_version == 0
    assert status.exists is False


def test_init_applies_every_migration_in_order(project: Path) -> None:
    """A fresh database must be identical to one that grew through the
    versions — otherwise 'current schema' is a second source of truth."""
    conn = sqlite3.connect(db_path(project))
    try:
        applied = [r[0] for r in conn.execute("SELECT version FROM schema_version")]
    finally:
        conn.close()
    assert applied == sorted(schema.MIGRATIONS)


# --- guard rails -----------------------------------------------------------


def test_older_database_refuses_to_open_and_names_the_fix(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening must never migrate silently — that would rewrite history
    the user has not backed up."""
    monkeypatch.setattr(schema, "SCHEMA_VERSION", schema.SCHEMA_VERSION + 1)
    monkeypatch.setattr(connection, "SCHEMA_VERSION", schema.SCHEMA_VERSION + 1)
    with pytest.raises(SchemaMismatchError) as exc:
        with get_db(project):
            pass
    assert "vibe-sentinel migrate" in str(exc.value)


def test_newer_database_says_upgrade_not_migrate(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migrating forward would be a no-op and dropping columns would
    destroy data, so neither is offered."""
    monkeypatch.setattr(connection, "SCHEMA_VERSION", 0)
    with pytest.raises(SchemaMismatchError) as exc:
        with get_db(project):
            pass
    message = str(exc.value)
    assert "NEWER" in message
    assert "Upgrade vibe-sentinel" in message


def test_migrating_an_absent_database_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="No history database"):
        run_migration(tmp_path)


def test_up_to_date_migration_is_a_no_op(project: Path) -> None:
    result = run_migration(project)
    assert result.success
    assert result.from_version == result.to_version
    assert result.backup_path is None


def test_locked_database_is_refused(project: Path) -> None:
    """Migrating a file another process is writing would interleave with
    its transaction."""
    holder = sqlite3.connect(str(db_path(project)))
    try:
        holder.execute("BEGIN EXCLUSIVE")
        with pytest.raises(MigrationError, match="locked"):
            migration._check_not_locked(db_path(project))
    finally:
        holder.rollback()
        holder.close()


# --- applying a real migration ---------------------------------------------


_NEXT_SQL = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    body TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_body ON notes(body);
"""

#: The synthetic migration sits one past whatever the real schema is at,
#: so adding a genuine migration never collides with these tests.
NEXT_VERSION = schema.SCHEMA_VERSION + 1


@pytest.fixture
def with_next(monkeypatch: pytest.MonkeyPatch):
    """Register a synthetic migration one past the real head version."""

    def _register(verify: list[str] | None = None) -> None:
        monkeypatch.setitem(schema.MIGRATIONS, NEXT_VERSION, _NEXT_SQL)
        monkeypatch.setitem(
            schema.VERIFY,
            NEXT_VERSION,
            verify
            if verify is not None
            else [
                "SELECT id, body FROM notes LIMIT 0",
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_notes_body'",
            ],
        )
        monkeypatch.setattr(schema, "SCHEMA_VERSION", NEXT_VERSION)
        monkeypatch.setattr(migration, "SCHEMA_VERSION", NEXT_VERSION)

    return _register


def test_migration_applies_and_verifies(project: Path, with_next) -> None:
    with_next()
    result = run_migration(project)
    assert result.success
    assert result.from_version == NEXT_VERSION - 1
    assert result.to_version == NEXT_VERSION

    conn = sqlite3.connect(db_path(project))
    try:
        conn.execute("SELECT id, body FROM notes LIMIT 0")
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_version")]
    finally:
        conn.close()
    assert versions == sorted(schema.MIGRATIONS)


def test_migration_preserves_existing_rows(project: Path, with_next) -> None:
    """The whole point: history survives the schema change."""
    conn = sqlite3.connect(db_path(project))
    try:
        conn.execute(
            "INSERT INTO runs (started_at, root, model, used_model)"
            " VALUES ('t', 'r', 'm', 1)"
        )
        conn.commit()
    finally:
        conn.close()

    with_next()
    assert run_migration(project).success

    conn = sqlite3.connect(db_path(project))
    try:
        rows = conn.execute("SELECT started_at, root FROM runs").fetchall()
    finally:
        conn.close()
    assert rows == [("t", "r")]


def test_migration_leaves_a_backup(project: Path, with_next) -> None:
    with_next()
    result = run_migration(project)
    assert result.backup_path is not None
    assert result.backup_path.is_file()
    assert f"pre-v{NEXT_VERSION}" in result.backup_path.name


def test_backup_is_the_pre_migration_database(project: Path, with_next) -> None:
    """No revert scripts — the backup is the revert."""
    with_next()
    result = run_migration(project)
    assert result.backup_path is not None
    assert read_schema_version(result.backup_path) == NEXT_VERSION - 1


def test_failed_verify_leaves_the_original_untouched(project: Path, with_next) -> None:
    """Copy-on-write: a migration that does not verify must not land."""
    with_next(verify=["SELECT column_that_does_not_exist FROM notes LIMIT 0"])
    result = run_migration(project)

    assert result.success is False
    assert "Verify failed" in result.error
    # Original still at v1, with no `notes` table.
    assert read_schema_version(db_path(project)) == NEXT_VERSION - 1
    conn = sqlite3.connect(db_path(project))
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            conn.execute("SELECT 1 FROM notes")
    finally:
        conn.close()


def test_migration_without_verify_queries_is_refused(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration nobody proved landed is not a migration."""
    monkeypatch.setitem(schema.MIGRATIONS, NEXT_VERSION, _NEXT_SQL)
    monkeypatch.setattr(schema, "SCHEMA_VERSION", NEXT_VERSION)
    monkeypatch.setattr(migration, "SCHEMA_VERSION", NEXT_VERSION)
    # VERIFY deliberately left without an entry for NEXT_VERSION.
    result = run_migration(project)
    assert result.success is False
    assert "no verify queries" in result.error


def test_failed_migration_removes_the_working_copy(project: Path, with_next) -> None:
    with_next(verify=["SELECT nope FROM notes LIMIT 0"])
    run_migration(project)
    leftovers = list(db_path(project).parent.glob("*.migrating*"))
    assert leftovers == []


def test_broken_sql_rolls_back(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(schema.MIGRATIONS, NEXT_VERSION, "CREATE TABLE bad (;\n")
    monkeypatch.setitem(schema.VERIFY, NEXT_VERSION, ["SELECT 1"])
    monkeypatch.setattr(schema, "SCHEMA_VERSION", NEXT_VERSION)
    monkeypatch.setattr(migration, "SCHEMA_VERSION", NEXT_VERSION)
    result = run_migration(project)
    assert result.success is False
    assert read_schema_version(db_path(project)) == NEXT_VERSION - 1


# --- sql splitting ---------------------------------------------------------


def test_sql_split_drops_comments_and_blanks() -> None:
    statements = migration._split_sql(
        "-- a comment\nCREATE TABLE a (id INTEGER);\n\n-- another\n"
        "CREATE INDEX i ON a(id);\n"
    )
    assert len(statements) == 2
    assert statements[0].startswith("-- a comment") or "CREATE TABLE" in statements[0]
    assert all(s.endswith(";") for s in statements)


def test_sql_split_of_empty_input_is_empty() -> None:
    assert migration._split_sql("\n\n  \n") == []


# --- backups ---------------------------------------------------------------


def test_backups_listed_newest_first(project: Path, with_next) -> None:
    with_next()
    run_migration(project)
    backups = list_backups(db_path(project).parent / BACKUP_DIR_NAME)
    assert len(backups) == 1
    assert backups[0].pre_version == NEXT_VERSION


def test_no_backups_dir_lists_nothing(tmp_path: Path) -> None:
    assert list_backups(tmp_path / "absent") == []


def test_cleanup_always_keeps_the_most_recent(tmp_path: Path) -> None:
    """A project scanned twice a year would otherwise migrate and delete
    the only copy of its previous history."""
    backups = tmp_path / BACKUP_DIR_NAME
    backups.mkdir()
    old = backups / "history.db.pre-v1.20200101-000000"
    old.write_text("x", encoding="utf-8")
    import os
    import time

    ancient = time.time() - 400 * 86400
    os.utime(old, (ancient, ancient))
    assert cleanup_old_backups(backups) == []
    assert old.is_file()


# --- v2: the agent command journal ----------------------------------------


def _v1_database(root: Path) -> Path:
    """A database at v1 — before the command journal existed."""
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for statement in migration._split_sql(schema.MIGRATIONS[1]):
            conn.execute(statement)
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.execute(
            "INSERT INTO runs (started_at, root, model, used_model)"
            " VALUES ('2026-01-01T00:00:00+00:00', 'src', 'qwen3', 1)"
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_v2_adds_the_journal_and_keeps_recorded_runs(tmp_path: Path) -> None:
    """The journal is additive: a project that scanned before it existed
    keeps every run it recorded."""
    _v1_database(tmp_path)
    assert read_schema_version(db_path(tmp_path)) == 1

    assert run_migration(tmp_path).success
    # Not hardcoded to 2: the claim is that the journal arrived and took
    # nothing with it, and that has to keep holding as versions accrue.
    assert read_schema_version(db_path(tmp_path)) == schema.SCHEMA_VERSION

    with get_db(tmp_path) as conn:
        runs = [tuple(r) for r in conn.execute("SELECT started_at, root FROM runs")]
        assert runs == [("2026-01-01T00:00:00+00:00", "src")]

        journal_store.record_command(
            conn,
            HookEvent(session_id="s", tool_name="Bash", tool_input={"command": "ls"}),
            "2026-01-02T00:00:00.000+00:00",
        )
        assert [c.command for c in journal_store.list_commands(conn)] == ["ls"]


def test_a_v1_database_refuses_to_open_until_migrated(tmp_path: Path) -> None:
    """A pending migration must be loud, not a silently empty journal."""
    _v1_database(tmp_path)
    with pytest.raises(SchemaMismatchError, match="vibe-sentinel migrate"):
        with get_db(tmp_path) as conn:
            conn.execute("SELECT 1")


def test_v4_adds_the_verdict_table_beside_the_commands(tmp_path: Path) -> None:
    """A verdict lives next to the command, never inside it: the record
    of what ran must not depend on anyone's opinion of it."""
    _v1_database(tmp_path)
    assert run_migration(tmp_path).success

    with get_db(tmp_path) as conn:
        journal_store.record_command(
            conn,
            HookEvent(
                session_id="s", tool_name="Bash", tool_input={"command": "rm -rf /"}
            ),
            "2026-01-02T00:00:00.000+00:00",
        )
        (command,) = journal_store.list_commands(conn)
        review_id = journal_store.save_review(
            conn,
            command_id=command.id,
            reviewed_at="2026-01-02T00:00:01.000+00:00",
            signals="deleting-files,filesystem-root",
            verdict="unsafe",
            reason="it removes the filesystem root",
            model="qwen3",
            reviewed=True,
            mode="enforce",
            enforced=True,
            history_count=12,
            duration_ms=740,
        )
        assert review_id is not None

        (row,) = journal_store.list_reviews(conn)
        assert row.verdict == "unsafe"
        assert row.enforced is True
        assert row.history_count == 12
        assert row.command == "rm -rf /"
        assert row.signal_list() == ["deleting-files", "filesystem-root"]

        # One verdict per command: re-reviewing does not overwrite what
        # the gate actually did at the time.
        assert (
            journal_store.save_review(
                conn,
                command_id=command.id,
                reviewed_at="2026-01-03T00:00:00.000+00:00",
                signals="delete",
                verdict="safe",
                reason="changed my mind",
                model="other",
                reviewed=True,
                mode="observe",
                enforced=False,
                history_count=0,
                duration_ms=1,
            )
            is None
        )
        assert len(journal_store.list_reviews(conn)) == 1


# --- the index declarations ------------------------------------------------


def test_a_migration_that_does_not_create_its_index_fails_verify(
    project: Path, with_next, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason EXPECT_INDEXES exists at all.

    A ``VERIFY`` query cannot check an index: ``SELECT 1 FROM
    sqlite_master WHERE name='idx_x'`` prepares whether or not ``idx_x``
    is there, and no rows is not an error. Declaring the name is the only
    check that fails when the index is missing.
    """
    with_next(verify=["SELECT id, body FROM notes LIMIT 0"])
    monkeypatch.setitem(schema.EXPECT_INDEXES, NEXT_VERSION, ["idx_notes_absent"])

    result = run_migration(project)

    assert not result.success
    assert "idx_notes_absent" in result.error
    assert read_schema_version(db_path(project)) == NEXT_VERSION - 1


def test_a_migration_that_leaves_a_superseded_index_fails_verify(
    project: Path, with_next, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creating the longer index and forgetting to drop its prefix passes
    every other check and still costs the writer what the drop saved."""
    with_next(verify=["SELECT id, body FROM notes LIMIT 0"])
    monkeypatch.setitem(schema.ABSENT_INDEXES, NEXT_VERSION, ["idx_notes_body"])

    result = run_migration(project)

    assert not result.success
    assert "idx_notes_body" in result.error


def test_declared_indexes_land_and_the_original_is_untouched(
    project: Path, with_next, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_next(verify=["SELECT id, body FROM notes LIMIT 0"])
    monkeypatch.setitem(schema.EXPECT_INDEXES, NEXT_VERSION, ["idx_notes_body"])

    assert run_migration(project).success

    conn = sqlite3.connect(db_path(project))
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()
    assert "idx_notes_body" in names


def test_head_indexes_is_what_a_fresh_database_actually_has(project: Path) -> None:
    """``head_indexes()`` drives the health check and ``db reindex``. If
    it disagrees with the migrations, both of them start lying."""
    conn = sqlite3.connect(db_path(project))
    try:
        present = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
            )
        }
    finally:
        conn.close()
    assert present == schema.head_indexes()


def _database_at(root: Path, version: int) -> Path:
    """A database at ``version``, with one run and one observation on it."""
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for v in range(1, version + 1):
            for statement in migration._split_sql(schema.MIGRATIONS[v]):
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (v,))
        conn.execute(
            "INSERT INTO runs (started_at, root, model, used_model)"
            " VALUES ('2026-01-01T00:00:00+00:00', 'src', 'qwen3', 1)"
        )
        conn.execute(
            "INSERT INTO probe_runs (run_id, probe_id, title, command_json,"
            " parameters_json, ok) VALUES (1, 'module-organization', 'Shape',"
            " '[\"echo\"]', '{}', 1)"
        )
        conn.execute(
            "INSERT INTO observations (run_id, probe_id, key, value, label)"
            " VALUES (1, 'module-organization', 'dir:src', 9.0, 'nine modules')"
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_v7_adds_state_and_leaves_every_recorded_measurement_alone(
    tmp_path: Path,
) -> None:
    """The column arrives empty on history that predates it, and that is
    the correct value: every probe that ran before this migration
    measured a magnitude and nothing else."""
    _database_at(tmp_path, 6)
    assert read_schema_version(db_path(tmp_path)) == 6

    assert run_migration(tmp_path).success
    assert read_schema_version(db_path(tmp_path)) == schema.SCHEMA_VERSION

    with get_db(tmp_path) as conn:
        row = conn.execute("SELECT key, value, state FROM observations").fetchone()
        assert (row["key"], row["value"], row["state"]) == ("dir:src", 9.0, "")


def test_a_migrated_database_reports_no_change_for_the_state_it_gained(
    tmp_path: Path,
) -> None:
    """The run after this migration must not announce every key in the
    history as having changed from nothing into its first state. That is
    a definition change, and reporting it as drift would put a wall of
    changes in front of whatever real drift that scan found."""
    from vibe_sentinel.db import store
    from vibe_sentinel.inventory import compare
    from vibe_sentinel.schemas import Observation, ProbeResult, Snapshot
    from vibe_sentinel.templates import Probe

    _database_at(tmp_path, 6)
    assert run_migration(tmp_path).success

    with get_db(tmp_path) as conn:
        baseline = store.load_snapshot(conn, 1)
    assert baseline is not None

    # The same measurement again, from a probe that now also records a
    # state — exactly what the first scan after an upgrade looks like.
    current = Snapshot(
        probes={
            "module-organization": ProbeResult(
                probe_id="module-organization",
                observations=[Observation(key="dir:src", value=9.0, state="whatever")],
            )
        }
    )
    probe = Probe(id="module-organization", title="t", command=["echo"])
    assert compare(baseline, current, [probe]).changes == []
