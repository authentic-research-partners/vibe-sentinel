"""Size, health, backup and retention for the history database.

The prune tests carry the same weight as the migration ones and for the
same reason: this is the only code in the project that deletes a record
on purpose. What it must never do is delete one nobody named.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vibe_sentinel.config import SentinelConfig
from vibe_sentinel.db import journal_store, maintenance, schema, store
from vibe_sentinel.db.connection import db_path, get_db, init_db
from vibe_sentinel.db.migration import (
    BACKUP_DIR_NAME,
    cleanup_old_backups,
    list_backups,
)
from vibe_sentinel.journal import HookEvent
from vibe_sentinel.schemas import DriftReport, Observation, ProbeResult, Snapshot


@pytest.fixture
def project(tmp_path: Path) -> Path:
    init_db(db_path(tmp_path))
    return tmp_path


@pytest.fixture
def config() -> SentinelConfig:
    return SentinelConfig()


def _days_ago(days: int) -> str:
    """A timestamp ``days`` old, with a minute of margin on top.

    The margin is what keeps these tests from sitting one clock tick from
    wrong. Ages are read back as ``(now - stamp).days``, which truncates,
    so a stamp of exactly N days reads as N-1 the moment the clock steps
    backwards by any amount at all — routine on WSL2, and it failed a full
    parallel run here on the 120-day backup assertion. A minute is far
    inside every threshold these tests use and far outside any step they
    will ever see.
    """
    return (datetime.now(UTC) - timedelta(days=days, minutes=1)).isoformat(
        timespec="milliseconds"
    )


def _event(
    command: str = "ls", *, session_id: str = "s1", agent_id: str = ""
) -> HookEvent:
    return HookEvent(
        hook_event_name="PreToolUse",
        session_id=session_id,
        agent_id=agent_id,
        agent_type="",
        prompt_id="p1",
        tool_use_id="",
        tool_name="Bash",
        cwd="/p",
        permission_mode="default",
        tool_input={"command": command},
    )


def _journal(project: Path, entries: list[tuple[str, int]]) -> None:
    """Record ``(command, age_in_days)`` pairs into the journal."""
    with get_db(project) as conn:
        for command, age in entries:
            journal_store.record_command(conn, _event(command), _days_ago(age))
        # record_command stamps the actor with "now"; age the row so the
        # session sweep can see it as old too.
        oldest = min(age for _, age in entries)
        conn.execute(
            "UPDATE agent_sessions SET last_seen_at = ?, first_seen_at = ?",
            (_days_ago(oldest), _days_ago(max(a for _, a in entries))),
        )
        conn.commit()


def _scan(project: Path, *, days_ago: int, baseline: bool = False) -> int:
    """Record one run, dated ``days_ago``."""
    snapshot = Snapshot(
        generated_at=_days_ago(days_ago), root=str(project), model="m", used_model=False
    )
    snapshot.probes["p"] = ProbeResult(
        probe_id="p",
        title="t",
        command=["true"],
        filled={},
        observations=[Observation(key="k", value=1.0)],
        summary="",
        ok=True,
    )
    with get_db(project) as conn:
        return store.save_run(conn, snapshot, DriftReport(), None, baseline)


# --- the index set -----------------------------------------------------


def test_a_fresh_database_carries_exactly_the_declared_indexes(project: Path) -> None:
    """The declaration and the DDL are two statements of one fact.

    ``head_indexes()`` is what the health check compares against and what
    ``reindex`` rebuilds from. If it drifts from the migrations, both of
    them start lying.
    """
    conn = sqlite3.connect(db_path(project))
    try:
        present = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
                " AND name LIKE 'idx_%'"
            )
        }
    finally:
        conn.close()
    assert present == schema.head_indexes()


def test_superseded_prefix_indexes_are_gone(project: Path) -> None:
    conn = sqlite3.connect(db_path(project))
    try:
        present = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    finally:
        conn.close()
    for dropped in (
        "idx_changes_key",
        "idx_agent_commands_tool",
        "idx_command_reviews_verdict",
    ):
        assert dropped not in present


def test_partial_indexes_really_are_partial(project: Path) -> None:
    """A partial index that lost its WHERE is a different, larger index."""
    conn = sqlite3.connect(db_path(project))
    try:
        sql = dict(
            conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
            )
        )
    finally:
        conn.close()
    for name in (
        "idx_runs_baseline",
        "idx_observations_risk",
        "idx_observations_run_risk",
        "idx_agent_commands_prompt",
        "idx_agent_commands_use_id",
    ):
        assert "WHERE" in sql[name].upper(), name


# --- measuring ---------------------------------------------------------


def test_measure_counts_every_table(project: Path) -> None:
    _journal(project, [("ls", 1)])
    with get_db(project) as conn:
        size = maintenance.measure(conn, db_path(project))

    counts = {t.name: t.rows for t in size.tables}
    assert counts["agent_commands"] == 1
    assert counts["agent_sessions"] == 1
    assert counts["runs"] == 0
    assert size.file_bytes > 0
    assert size.page_size > 0


def test_measure_attributes_index_pages_to_their_table(project: Path) -> None:
    for i in range(200):
        _journal(project, [(f"cmd {i}", 1)])
    with get_db(project) as conn:
        size = maintenance.measure(conn, db_path(project))

    if not size.detailed:  # pragma: no cover - dbstat is compiled in here
        pytest.skip("this SQLite build has no dbstat")
    commands = next(t for t in size.tables if t.name == "agent_commands")
    assert commands.table_bytes > 0
    assert commands.index_bytes > 0
    assert commands.total_bytes == commands.table_bytes + commands.index_bytes


# --- checking ----------------------------------------------------------


def test_a_fresh_database_needs_no_attention(
    project: Path, config: SentinelConfig
) -> None:
    with get_db(project) as conn:
        report = maintenance.check(conn, project, config)
    assert report.ok
    assert report.integrity_ok is True
    assert [f.code for f in report.attention] == []


def test_a_dropped_index_is_reported(project: Path, config: SentinelConfig) -> None:
    with get_db(project) as conn:
        conn.execute("DROP INDEX idx_observations_trend")
        conn.commit()
        report = maintenance.check(conn, project, config)

    assert not report.ok
    finding = next(f for f in report.findings if f.code == "indexes")
    assert "idx_observations_trend" in finding.message
    assert "reindex" in finding.remediation


def test_a_skipped_integrity_scan_is_not_a_clean_bill(
    project: Path, config: SentinelConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`integrity_ok is None` means 'not checked', never 'checked, fine'."""
    monkeypatch.setattr(maintenance, "_AUTO_INTEGRITY_MAX_BYTES", 0)
    with get_db(project) as conn:
        report = maintenance.check(conn, project, config, full=False)

    assert report.integrity_ok is None
    assert any(f.code == "integrity-skipped" for f in report.findings)


def test_full_check_scans_integrity_however_large(
    project: Path, config: SentinelConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(maintenance, "_AUTO_INTEGRITY_MAX_BYTES", 0)
    with get_db(project) as conn:
        report = maintenance.check(conn, project, config, full=True)
    assert report.integrity_ok is True


def test_declared_ceilings_are_what_produce_a_size_finding(project: Path) -> None:
    """Nothing is reported until a project declares what 'too big' means."""
    with get_db(project) as conn:
        silent = maintenance.check(conn, project, SentinelConfig())
        loud = maintenance.check(
            conn, project, SentinelConfig(db_max_size_mb=0, db_max_journal_commands=0)
        )
        declared = maintenance.check(
            conn, project, SentinelConfig(db_max_size_mb=1, db_backup_max_age_days=0)
        )

    assert not any(f.code == "size" for f in silent.findings)
    assert not any(f.code == "size" for f in loud.findings)
    # A fresh database is well under 1 MB, so even a declared ceiling is quiet.
    assert not any(f.code == "size" for f in declared.findings)


def test_a_declared_journal_ceiling_is_reported(project: Path) -> None:
    _journal(project, [("a", 1), ("b", 1), ("c", 1)])
    with get_db(project) as conn:
        report = maintenance.check(
            conn, project, SentinelConfig(db_max_journal_commands=2)
        )
    finding = next(f for f in report.findings if f.code == "journal")
    assert "prune" in finding.remediation


def test_a_new_database_is_not_nagged_about_backups(project: Path) -> None:
    """There is nothing here yet that a backup would have saved, and a
    check that complains on someone's first command gets turned off."""
    with get_db(project) as conn:
        report = maintenance.check(
            conn, project, SentinelConfig(db_backup_max_age_days=30)
        )
    assert not any(f.code == "backup" for f in report.findings)


def test_history_older_than_the_threshold_with_no_backup_is_reported(
    project: Path,
) -> None:
    _scan(project, days_ago=120)
    with get_db(project) as conn:
        report = maintenance.check(
            conn, project, SentinelConfig(db_backup_max_age_days=30)
        )
    finding = next(f for f in report.findings if f.code == "backup")
    assert finding.severity == "warning"
    assert "120 days of history" in finding.message
    assert finding.remediation == "vibe-sentinel db backup"


def test_a_recent_backup_silences_the_backup_finding(project: Path) -> None:
    _scan(project, days_ago=120)
    maintenance.backup(project)
    with get_db(project) as conn:
        report = maintenance.check(
            conn, project, SentinelConfig(db_backup_max_age_days=30)
        )
    assert not any(f.code == "backup" for f in report.findings)


def test_declared_retention_reports_what_is_over_it(project: Path) -> None:
    _journal(project, [("old", 120), ("new", 1)])
    with get_db(project) as conn:
        report = maintenance.check(
            conn, project, SentinelConfig(db_journal_retention_days=90)
        )
    finding = next(f for f in report.findings if f.code == "retention")
    assert "1 journal command" in finding.message
    assert "--older-than 90" in finding.remediation


def test_every_finding_names_the_command_that_fixes_it(project: Path) -> None:
    _journal(project, [("old", 120)])
    _scan(project, days_ago=120)
    with get_db(project) as conn:
        conn.execute("DROP INDEX idx_changes_run")
        conn.commit()
        report = maintenance.check(
            conn,
            project,
            SentinelConfig(db_journal_retention_days=1, db_backup_max_age_days=1),
        )
    assert report.findings
    for finding in report.findings:
        assert finding.remediation, finding.code


# --- the maintenance record --------------------------------------------


def test_upkeep_is_recorded_and_read_back(
    project: Path, config: SentinelConfig
) -> None:
    with get_db(project) as conn:
        report = maintenance.check(conn, project, config)
        maintenance.record(
            conn, "health", ok=report.ok, size=report.size, findings=report.findings
        )
        assert maintenance.last_performed(conn, "health") is not None
        assert maintenance.last_performed(conn, "prune") is None
        recent = maintenance.recent_maintenance(conn)

    assert len(recent) == 1
    assert recent[0][1] == "health"


def test_is_due_before_the_first_check_and_not_straight_after(project: Path) -> None:
    with get_db(project) as conn:
        assert maintenance.is_due(conn, "health", 24)
        maintenance.record(conn, "health", ok=True)
        assert not maintenance.is_due(conn, "health", 24)
        assert maintenance.is_due(conn, "health", 0)


def test_an_unreadable_stamp_does_not_disable_checking_forever(project: Path) -> None:
    with get_db(project) as conn:
        conn.execute(
            "INSERT INTO db_maintenance (performed_at, kind, ok, size_bytes,"
            " wal_bytes, free_bytes, findings_json, detail, duration_ms)"
            " VALUES ('not a date', 'health', 1, 0, 0, 0, '[]', '', 0)"
        )
        conn.commit()
        assert maintenance.is_due(conn, "health", 24)


# --- the automatic check -----------------------------------------------


def test_maybe_check_runs_once_then_holds_off(
    project: Path, config: SentinelConfig
) -> None:
    first = maintenance.maybe_check(project, config)
    second = maintenance.maybe_check(project, config)

    assert first is not None
    assert second is None
    with get_db(project) as conn:
        assert len(maintenance.recent_maintenance(conn)) == 1


def test_maybe_check_runs_again_once_the_interval_passes(project: Path) -> None:
    assert maintenance.maybe_check(project, SentinelConfig()) is not None
    assert (
        maintenance.maybe_check(project, SentinelConfig(db_check_interval_hours=0))
        is not None
    )


def test_maybe_check_is_off_when_declared_off(project: Path) -> None:
    assert maintenance.maybe_check(project, SentinelConfig(db_auto_check=False)) is None


def test_maybe_check_is_silent_without_a_database(tmp_path: Path) -> None:
    assert maintenance.maybe_check(tmp_path, SentinelConfig()) is None


def test_maybe_check_reports_a_pending_migration_without_opening(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_db refuses an out-of-date file, so the check cannot record —
    but the one thing it can establish is worth more than silence."""
    next_version = schema.SCHEMA_VERSION + 1
    monkeypatch.setitem(schema.MIGRATIONS, next_version, "CREATE TABLE later (x);")
    monkeypatch.setattr(schema, "SCHEMA_VERSION", next_version)
    monkeypatch.setattr("vibe_sentinel.db.migration.SCHEMA_VERSION", next_version)

    report = maintenance.maybe_check(project, SentinelConfig())

    assert report is not None
    assert [f.code for f in report.attention] == ["schema"]
    assert report.attention[0].remediation == "vibe-sentinel migrate"

    # And nothing was written: at this version the file cannot be opened
    # at all, so the check that could not run must not look like one that
    # ran and found only this.
    monkeypatch.undo()
    with get_db(project) as conn:
        assert maintenance.recent_maintenance(conn) == []


def test_maybe_check_never_raises(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It runs in front of an unrelated command and must not be what
    stops one."""

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(maintenance, "check", boom)
    assert maintenance.maybe_check(project, SentinelConfig()) is None


# --- backup ------------------------------------------------------------


def test_a_backup_is_a_complete_readable_database(project: Path) -> None:
    _journal(project, [("a", 1), ("b", 1)])
    _scan(project, days_ago=1)

    result = maintenance.backup(project)

    assert result.path.is_file()
    conn = sqlite3.connect(result.path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM agent_commands").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_a_backup_can_be_taken_while_a_writer_holds_the_database(project: Path) -> None:
    """The whole reason for the online backup API rather than a file copy."""
    _journal(project, [("a", 1)])
    with get_db(project) as writer:
        writer.execute("BEGIN")
        writer.execute(
            "INSERT INTO db_maintenance (performed_at, kind, ok, size_bytes,"
            " wal_bytes, free_bytes, findings_json, detail, duration_ms)"
            " VALUES ('x', 'health', 1, 0, 0, 0, '[]', '', 0)"
        )
        result = maintenance.backup(project)
        writer.rollback()

    conn = sqlite3.connect(result.path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # The uncommitted row is not in the copy, which is the point.
        assert conn.execute("SELECT COUNT(*) FROM db_maintenance").fetchone()[0] == 0
    finally:
        conn.close()


def test_an_explicit_output_path_is_honoured(project: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere" / "history-copy.db"
    result = maintenance.backup(project, target)
    assert result.path == target
    assert target.is_file()


def test_backup_leaves_no_partial_file_behind(project: Path) -> None:
    maintenance.backup(project)
    leftovers = list((db_path(project).parent / BACKUP_DIR_NAME).glob("*.partial"))
    assert leftovers == []


def test_retention_keeps_the_newest_of_each_kind(project: Path) -> None:
    """A deliberate backup must not evaporate because a migration ran."""
    backups_dir = db_path(project).parent / BACKUP_DIR_NAME
    backups_dir.mkdir(parents=True, exist_ok=True)

    # Two distinct times, not one time with its last character swapped:
    # `stamp[:-1] + "9"` is the same string whenever the seconds already end
    # in 9, which collides the pair into one file and leaves nothing to
    # discard. That happened on one run in ten.
    older = datetime.now(UTC) - timedelta(days=400)
    newer = older + timedelta(minutes=1)
    for moment, names in (
        (
            older,
            ("history.db.backup.{}", "history.db.pre-v2.{}"),
        ),
        (
            newer,
            ("history.db.backup.{}", "history.db.pre-v3.{}"),
        ),
    ):
        stamp = moment.strftime("%Y%m%d-%H%M%S")
        for template in names:
            path = backups_dir / template.format(stamp)
            path.write_bytes(b"x")
            # Ordering comes from the mtime, so it has to differ too —
            # equal mtimes left "which one survives" to the filesystem.
            os.utime(path, (moment.timestamp(), moment.timestamp()))

    manual = backups_dir / "something-a-person-put-here"
    manual.write_bytes(b"x")
    os.utime(manual, (older.timestamp(), older.timestamp()))

    removed = cleanup_old_backups(backups_dir)
    surviving = {b.path.name for b in list_backups(backups_dir)}

    assert len(removed) == 2  # one manual, one migration — the older of each
    assert "something-a-person-put-here" in surviving
    assert sum(1 for n in surviving if ".backup." in n) == 1
    assert sum(1 for n in surviving if ".pre-v" in n) == 1
    # And it is the newer of each that survived, which equal mtimes could
    # never actually establish.
    kept = newer.strftime("%Y%m%d-%H%M%S")
    assert f"history.db.backup.{kept}" in surviving
    assert f"history.db.pre-v3.{kept}" in surviving


# --- pruning -----------------------------------------------------------


def test_a_dry_run_deletes_nothing(project: Path) -> None:
    _journal(project, [("old", 120), ("new", 1)])
    with get_db(project) as conn:
        result = maintenance.prune(
            conn, project, cutoff=maintenance.cutoff_from_days(90)
        )
        remaining = conn.execute("SELECT COUNT(*) FROM agent_commands").fetchone()[0]

    assert result.applied is False
    assert result.deleted["agent_commands"] == 1
    assert remaining == 2
    assert result.backup_path is None


def test_applying_deletes_only_what_is_older_than_the_cutoff(project: Path) -> None:
    _journal(project, [("old", 120), ("older", 200), ("new", 1)])
    with get_db(project) as conn:
        result = maintenance.prune(
            conn, project, cutoff=maintenance.cutoff_from_days(90), apply=True
        )
        survivors = [
            r[0] for r in conn.execute("SELECT command FROM agent_commands ORDER BY id")
        ]

    assert result.applied is True
    assert result.deleted["agent_commands"] == 2
    assert survivors == ["new"]


def test_an_applied_prune_backs_up_first(project: Path) -> None:
    _journal(project, [("old", 120)])
    with get_db(project) as conn:
        result = maintenance.prune(
            conn, project, cutoff=maintenance.cutoff_from_days(90), apply=True
        )

    assert result.backup_path is not None
    assert result.backup_path.is_file()
    conn = sqlite3.connect(result.backup_path)
    try:
        # The backup predates the delete — that is what makes it a revert.
        assert conn.execute("SELECT COUNT(*) FROM agent_commands").fetchone()[0] == 1
    finally:
        conn.close()


def test_a_prune_with_nothing_to_do_takes_no_backup(project: Path) -> None:
    _journal(project, [("new", 1)])
    with get_db(project) as conn:
        result = maintenance.prune(
            conn, project, cutoff=maintenance.cutoff_from_days(90), apply=True
        )
    assert result.total == 0
    assert result.backup_path is None
    assert list_backups(db_path(project).parent / BACKUP_DIR_NAME) == []


def test_the_actor_count_is_recomputed_to_what_survives(project: Path) -> None:
    _journal(project, [("old", 120), ("new", 1)])
    with get_db(project) as conn:
        maintenance.prune(
            conn, project, cutoff=maintenance.cutoff_from_days(90), apply=True
        )
        actors = journal_store.list_agent_sessions(conn)

    assert len(actors) == 1
    assert actors[0].command_count == 1


def test_an_actor_left_with_nothing_is_removed(project: Path) -> None:
    _journal(project, [("old", 120), ("older", 130)])
    with get_db(project) as conn:
        result = maintenance.prune(
            conn, project, cutoff=maintenance.cutoff_from_days(90), apply=True
        )
        assert conn.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0] == 0
    assert result.deleted["agent_sessions"] == 1


def test_the_journal_prune_never_touches_the_scan_history(project: Path) -> None:
    _scan(project, days_ago=400)
    _journal(project, [("old", 400)])
    with get_db(project) as conn:
        result = maintenance.prune(
            conn, project, cutoff=maintenance.cutoff_from_days(90), apply=True
        )
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1

    assert "runs" not in result.deleted


def test_scans_are_deleted_only_when_asked(project: Path) -> None:
    for _ in range(14):
        _scan(project, days_ago=400)
    with get_db(project) as conn:
        result = maintenance.prune(
            conn,
            project,
            cutoff=maintenance.cutoff_from_days(90),
            scans=True,
            keep_runs=10,
            apply=True,
        )
        remaining = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    assert result.deleted["runs"] == 4
    assert remaining == 10


def test_the_baseline_run_is_never_deleted(project: Path) -> None:
    baseline = _scan(project, days_ago=400, baseline=True)
    for _ in range(14):
        _scan(project, days_ago=400)
    with get_db(project) as conn:
        maintenance.prune(
            conn,
            project,
            cutoff=maintenance.cutoff_from_days(90),
            scans=True,
            keep_runs=2,
            apply=True,
        )
        survivors = [r[0] for r in conn.execute("SELECT id FROM runs")]

    assert baseline in survivors


def test_deleting_a_run_takes_its_observations_with_it(project: Path) -> None:
    for _ in range(3):
        _scan(project, days_ago=400)
    with get_db(project) as conn:
        before = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        maintenance.prune(
            conn,
            project,
            cutoff=maintenance.cutoff_from_days(90),
            scans=True,
            keep_runs=1,
            apply=True,
        )
        after = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        orphans = conn.execute(
            "SELECT COUNT(*) FROM observations o"
            " WHERE NOT EXISTS (SELECT 1 FROM runs r WHERE r.id = o.run_id)"
        ).fetchone()[0]

    assert before == 3
    assert after == 1
    assert orphans == 0


def test_cutoff_from_days_is_the_shape_the_columns_store(project: Path) -> None:
    cutoff = maintenance.cutoff_from_days(90)
    assert datetime.fromisoformat(cutoff).tzinfo is not None
    # Comparable as text against occurred_at, which is what the query does.
    assert cutoff < _days_ago(0)
    assert cutoff > _days_ago(120)


# --- vacuum and reindex ------------------------------------------------


def test_vacuum_reclaims_free_pages(project: Path) -> None:
    _journal(project, [(f"command number {i}" * 20, 1) for i in range(500)])
    with get_db(project) as conn:
        conn.execute("DELETE FROM agent_commands")
        conn.commit()

    before, after = maintenance.vacuum(project)
    assert after < before


def test_vacuum_backs_up_first(project: Path) -> None:
    _journal(project, [("a", 1)])
    maintenance.vacuum(project)
    assert list_backups(db_path(project).parent / BACKUP_DIR_NAME)


def test_reindex_recreates_a_dropped_index(project: Path) -> None:
    with get_db(project) as conn:
        conn.execute("DROP INDEX idx_observations_trend")
        conn.execute("DROP INDEX idx_agent_commands_tool_id")
        conn.commit()

    created = maintenance.reindex(project)

    assert set(created) == {"idx_observations_trend", "idx_agent_commands_tool_id"}
    conn = sqlite3.connect(db_path(project))
    try:
        present = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    finally:
        conn.close()
    assert schema.head_indexes() <= present


def test_reindex_does_not_resurrect_a_superseded_index(project: Path) -> None:
    """The migrations still contain the old CREATE statements; v5's DROPs
    are what decide. Re-running the creates must not undo them."""
    with get_db(project) as conn:
        conn.execute("DROP INDEX idx_changes_key_run")
        conn.commit()

    maintenance.reindex(project)

    conn = sqlite3.connect(db_path(project))
    try:
        present = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    finally:
        conn.close()
    assert "idx_changes_key_run" in present
    assert "idx_changes_key" not in present


def test_reindex_is_a_no_op_when_nothing_is_missing(project: Path) -> None:
    assert maintenance.reindex(project) == []
