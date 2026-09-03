"""Size, health, backup and retention for the history database.

Four jobs, one module, because they are the same question asked four
ways: *is this file still in good shape, and what would it cost to keep
it that way.*

  - :func:`measure`  — how big, broken down by table and index.
  - :func:`check`    — what needs attention, each finding with the
                       command that fixes it.
  - :func:`backup`   — an online copy, safe to take while a scan writes.
  - :func:`prune`    — trim old records, dry-run first, backup always.

Every one of them records what it did in ``db_maintenance``. That table
is the answer to "when was this last looked at", and it lives inside the
database rather than beside it so it travels with the file — into a
backup, through a migration, across a restore.

**Nothing here rebuilds the database.** :func:`vacuum` rewrites the file
in place to reclaim free pages and :func:`prune` deletes rows the caller
explicitly asked for; neither ever recreates a schema or discards a row
nobody named. The rule holds here as everywhere else — this module is
upkeep, not a reset.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field

from vibe_sentinel.db.connection import db_path
from vibe_sentinel.db.migration import (
    BACKUP_DIR_NAME,
    cleanup_old_backups,
    get_status,
    list_backups,
)
from vibe_sentinel.db.schema import head_indexes

#: The tables a health check counts rows in, in the order they are shown:
#: the scan history first, then the journal that annotates it.
TABLES: tuple[str, ...] = (
    "runs",
    "probe_runs",
    "observations",
    "changes",
    "gate_runs",
    "gate_findings",
    "agent_sessions",
    "agent_commands",
    "command_reviews",
    "db_maintenance",
)

#: Journal tables — high-volume operational records, written once per
#: tool call. What :func:`prune` trims unless told otherwise.
JOURNAL_TABLES: tuple[str, ...] = (
    "agent_commands",
    "agent_sessions",
    "command_reviews",
)

#: Scan tables — the structural history. What :func:`prune` will only
#: touch when explicitly asked, because it is the one thing here that
#: cannot be re-measured: probes can be re-run, but only against the code
#: as it is now.
SCAN_TABLES: tuple[str, ...] = (
    "runs",
    "probe_runs",
    "observations",
    "changes",
    "gate_runs",
    "gate_findings",
)

#: Above this, the *automatic* check skips the integrity scan, which
#: reads every page. An explicit ``vibe-sentinel db check`` still runs it.
#: A daily check that stalls a command for ten seconds gets turned off,
#: and a check nobody runs finds nothing.
_AUTO_INTEGRITY_MAX_BYTES = 256 * 1024 * 1024

#: Free space worth reclaiming, as a fraction of the file and as a floor
#: in bytes. Both must be exceeded: 40% of a 200 KB file is not worth a
#: rewrite, and 2 MB of a 2 GB file is not worth mentioning.
_FRAGMENT_RATIO = 0.25
_FRAGMENT_MIN_BYTES = 4 * 1024 * 1024

#: A WAL this large means checkpoints are not completing — usually a
#: reader that never closes its connection.
_WAL_WARN_BYTES = 64 * 1024 * 1024

_SEVERITY_RANK = {"critical": 0, "warning": 1, "notice": 2}


class TableSize(BaseModel):
    """One table's footprint: its own pages, and its indexes' pages."""

    name: str
    rows: int
    table_bytes: int = 0
    index_bytes: int = 0

    @property
    def total_bytes(self) -> int:
        return self.table_bytes + self.index_bytes


class DatabaseSize(BaseModel):
    """What the database weighs, and how that weight is distributed."""

    path: Path
    file_bytes: int
    wal_bytes: int
    page_size: int
    page_count: int
    freelist_count: int
    tables: list[TableSize] = Field(default_factory=list)
    #: False when this build of SQLite has no ``dbstat``, in which case
    #: per-table byte counts are absent and only row counts are known.
    detailed: bool = True

    @property
    def free_bytes(self) -> int:
        return self.freelist_count * self.page_size

    @property
    def total_bytes(self) -> int:
        """File plus WAL — what the directory actually holds."""
        return self.file_bytes + self.wal_bytes


class Finding(BaseModel):
    """Something that needs attention, and the command that fixes it."""

    code: str
    severity: str
    message: str
    remediation: str = ""


class HealthReport(BaseModel):
    """One health check, as reported at the time."""

    at: str
    size: DatabaseSize
    findings: list[Finding] = Field(default_factory=list)
    schema_current: int
    schema_target: int
    #: None when the scan was skipped — a large file under the automatic
    #: check. Never conflate "not checked" with "checked and clean": the
    #: same rule as ``runs.analyzed`` and ``command_reviews.reviewed``.
    integrity_ok: bool | None = None
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        """True when nothing warrants attention. Notices do not count."""
        return not self.attention

    @property
    def attention(self) -> list[Finding]:
        """Findings worth interrupting someone for, worst first."""
        return sorted(
            (f for f in self.findings if f.severity != "notice"),
            key=lambda f: _SEVERITY_RANK.get(f.severity, 9),
        )


class BackupResult(BaseModel):
    """One copy of the database, taken online."""

    path: Path
    size_bytes: int
    duration_ms: int


class PruneResult(BaseModel):
    """What a prune removed, or would remove."""

    applied: bool
    cutoff: str
    scope: list[str] = Field(default_factory=list)
    deleted: dict[str, int] = Field(default_factory=dict)
    kept_runs: list[int] = Field(default_factory=list)
    backup_path: Path | None = None
    freed_bytes: int = 0

    @property
    def total(self) -> int:
        return sum(self.deleted.values())


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------


def _file_bytes(path: Path) -> int:
    return path.stat().st_size if path.is_file() else 0


def measure(conn: sqlite3.Connection, path: Path) -> DatabaseSize:
    """Size the database: file, WAL, free pages, and per-table bytes.

    Per-table bytes come from ``dbstat``, which reads the b-tree page
    layout — the only way to say how much of a file one table occupies.
    It is a compile-time option; where it is absent the row counts are
    still exact and ``detailed`` says the byte columns are not there.
    """
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])

    rows: dict[str, int] = {}
    for table in TABLES:
        # ``table`` comes from the TABLES constant below, never from a caller.
        # A table name is an identifier, and no placeholder can carry one.
        try:
            rows[table] = int(
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # nosec B608
            )
        except sqlite3.OperationalError:
            # A table this build knows about but this file predates.
            # Reporting 0 would claim it is empty; omitting it is honest.
            continue

    table_bytes: dict[str, int] = {}
    index_bytes: dict[str, int] = {}
    detailed = True
    try:
        owner = {
            name: tbl
            for name, tbl in conn.execute(
                "SELECT name, tbl_name FROM sqlite_master WHERE type='index'"
            )
        }
        for name, pages in conn.execute(
            "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"
        ):
            if name in owner:
                index_bytes[owner[name]] = index_bytes.get(owner[name], 0) + int(pages)
            else:
                table_bytes[name] = table_bytes.get(name, 0) + int(pages)
    except sqlite3.OperationalError:
        logger.debug("dbstat unavailable — reporting row counts without bytes")
        detailed = False

    return DatabaseSize(
        path=path,
        file_bytes=_file_bytes(path),
        wal_bytes=_file_bytes(path.parent / f"{path.name}-wal"),
        page_size=page_size,
        page_count=page_count,
        freelist_count=freelist,
        detailed=detailed,
        tables=[
            TableSize(
                name=name,
                rows=count,
                table_bytes=table_bytes.get(name, 0),
                index_bytes=index_bytes.get(name, 0),
            )
            for name, count in rows.items()
        ],
    )


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------


def _oldest(conn: sqlite3.Connection, table: str, column: str) -> str | None:
    # Both arguments are identifiers, and both are literals at every call site
    # in this module. Same reason as above: an identifier cannot be bound.
    row = conn.execute(f"SELECT MIN({column}) FROM {table}").fetchone()  # nosec B608
    return str(row[0]) if row and row[0] is not None else None


def _parse(stamp: str, fallback: datetime) -> datetime:
    """An ISO timestamp from the database, as an aware datetime."""
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return fallback
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _oldest_record(conn: sqlite3.Connection) -> str | None:
    """The earliest thing this database remembers, of any kind.

    None when it remembers nothing yet — a file that exists but has never
    recorded a scan or a tool call. There is no history to lose, so the
    checks that ask "how long has this been at risk" have no question.
    """
    stamps = [
        s
        for s in (
            _oldest(conn, "runs", "started_at"),
            _oldest(conn, "agent_commands", "occurred_at"),
        )
        if s
    ]
    return min(stamps) if stamps else None


def _rows_before(conn: sqlite3.Connection, cutoff: str) -> int:
    """Journal commands older than ``cutoff``."""
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM agent_commands WHERE occurred_at < ?", (cutoff,)
        ).fetchone()[0]
    )


def check(
    conn: sqlite3.Connection,
    root: Path,
    config: object,
    *,
    full: bool = True,
) -> HealthReport:
    """Inspect the database and report what needs attention.

    ``full`` runs the integrity scan unconditionally. The automatic daily
    check passes ``full=False``, which skips it on a large file rather
    than making every command wait — and says so, rather than reporting a
    clean bill it never earned.
    """
    started = time.monotonic()
    now = datetime.now(UTC)
    size = measure(conn, db_path(root))
    findings: list[Finding] = []

    def add(code: str, severity: str, message: str, remediation: str = "") -> None:
        findings.append(
            Finding(
                code=code, severity=severity, message=message, remediation=remediation
            )
        )

    status = get_status(root)

    # --- integrity -------------------------------------------------------
    integrity_ok: bool | None = None
    if full or size.file_bytes <= _AUTO_INTEGRITY_MAX_BYTES:
        result = [r[0] for r in conn.execute("PRAGMA quick_check").fetchall()]
        integrity_ok = result == ["ok"]
        if not integrity_ok:
            add(
                "integrity",
                "critical",
                f"Integrity check failed: {'; '.join(result[:3])}",
                "Restore the newest good copy: vibe-sentinel backups, then "
                "copy one back over the database file.",
            )
    else:
        add(
            "integrity-skipped",
            "notice",
            f"Integrity scan skipped — the file is "
            f"{_human(size.file_bytes)} and this was the automatic check.",
            "Run it deliberately: vibe-sentinel db check",
        )

    # --- indexes ---------------------------------------------------------
    present = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    absent = sorted(head_indexes() - present)
    if absent:
        add(
            "indexes",
            "warning",
            f"{len(absent)} declared index(es) missing: {', '.join(absent)}",
            "Rebuild them: vibe-sentinel db reindex",
        )

    # --- free space ------------------------------------------------------
    ratio = size.freelist_count / size.page_count if size.page_count else 0.0
    if ratio >= _FRAGMENT_RATIO and size.free_bytes >= _FRAGMENT_MIN_BYTES:
        add(
            "fragmentation",
            "notice",
            f"{_human(size.free_bytes)} of {_human(size.file_bytes)} "
            f"({ratio:.0%}) is free pages left by deletes.",
            "Reclaim it: vibe-sentinel db vacuum",
        )

    # --- WAL -------------------------------------------------------------
    if size.wal_bytes >= _WAL_WARN_BYTES:
        add(
            "wal",
            "warning",
            f"The write-ahead log has grown to {_human(size.wal_bytes)}, "
            f"which means checkpoints are not completing.",
            "Close any process holding the database open, then: "
            "vibe-sentinel db vacuum",
        )

    # --- declared ceilings -----------------------------------------------
    retention = int(getattr(config, "db_journal_retention_days", 0) or 0)
    trim = (
        f"vibe-sentinel db prune --older-than {retention} --apply"
        if retention
        else "Declare [database] journal_retention_days, then: vibe-sentinel db prune"
    )

    max_mb = int(getattr(config, "db_max_size_mb", 0) or 0)
    if max_mb and size.total_bytes > max_mb * 1024 * 1024:
        add(
            "size",
            "warning",
            f"The database is {_human(size.total_bytes)}, over the "
            f"{max_mb} MB declared in [database] max_size_mb.",
            trim,
        )

    counts = {t.name: t.rows for t in size.tables}
    max_commands = int(getattr(config, "db_max_journal_commands", 0) or 0)
    recorded = counts.get("agent_commands", 0)
    if max_commands and recorded > max_commands:
        add(
            "journal",
            "warning",
            f"The journal holds {recorded:,} commands, over the "
            f"{max_commands:,} declared in [database] max_journal_commands.",
            trim,
        )

    # --- declared retention ----------------------------------------------
    if retention:
        cutoff = (now - timedelta(days=retention)).isoformat(timespec="seconds")
        stale = _rows_before(conn, cutoff)
        if stale:
            add(
                "retention",
                "notice",
                f"{stale:,} journal command(s) are older than the "
                f"{retention}-day retention declared in [database].",
                trim,
            )

    # --- backups ---------------------------------------------------------
    backup_days = int(getattr(config, "db_backup_max_age_days", 0) or 0)
    oldest_record = _oldest_record(conn)
    if backup_days and oldest_record is not None:
        # Measured against the history at risk, not against the clock. A
        # database created this morning has nothing a backup would have
        # saved, and telling its owner otherwise on their first command
        # is how a check earns its way into the ignore pile.
        history_days = (now - _parse(oldest_record, now)).days
        backups = list_backups(db_path(root).parent / BACKUP_DIR_NAME)
        if not backups:
            if history_days > backup_days:
                add(
                    "backup",
                    "warning",
                    f"{history_days} days of history and no backup. This "
                    f"file is the one artifact here that cannot be "
                    f"regenerated.",
                    "vibe-sentinel db backup",
                )
        else:
            age = (now - _parse(backups[0].created_at, now)).days
            if age > backup_days and history_days > backup_days:
                add(
                    "backup",
                    "warning",
                    f"The newest backup is {age} days old, past the "
                    f"{backup_days} declared in [database].",
                    "vibe-sentinel db backup",
                )

    # --- schema ----------------------------------------------------------
    if not status.up_to_date:
        add(
            "schema",
            "warning",
            f"Schema is at v{status.current_version}; this build expects "
            f"v{status.target_version}.",
            "vibe-sentinel migrate",
        )

    return HealthReport(
        at=now.isoformat(timespec="seconds"),
        size=size,
        findings=findings,
        schema_current=status.current_version,
        schema_target=status.target_version,
        integrity_ok=integrity_ok,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


# ---------------------------------------------------------------------------
# The maintenance record
# ---------------------------------------------------------------------------


def record(
    conn: sqlite3.Connection,
    kind: str,
    *,
    ok: bool,
    size: DatabaseSize | None = None,
    findings: list[Finding] | None = None,
    detail: str = "",
    duration_ms: int = 0,
) -> int:
    """Append one row to ``db_maintenance``. Its own transaction."""
    conn.execute("BEGIN")
    try:
        cursor = conn.execute(
            "INSERT INTO db_maintenance (performed_at, kind, ok, size_bytes,"
            " wal_bytes, free_bytes, findings_json, detail, duration_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(UTC).isoformat(timespec="seconds"),
                kind,
                int(ok),
                size.file_bytes if size else 0,
                size.wal_bytes if size else 0,
                size.free_bytes if size else 0,
                json.dumps([f.model_dump() for f in (findings or [])], sort_keys=True),
                detail,
                duration_ms,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return int(cursor.lastrowid or 0)


def last_performed(conn: sqlite3.Connection, kind: str) -> str | None:
    """When upkeep of ``kind`` last ran, or None if it never has."""
    row = conn.execute(
        "SELECT performed_at FROM db_maintenance WHERE kind = ?"
        " ORDER BY performed_at DESC LIMIT 1",
        (kind,),
    ).fetchone()
    return str(row[0]) if row else None


def recent_maintenance(
    conn: sqlite3.Connection, limit: int = 10
) -> list[tuple[str, str, bool, int, str]]:
    """Recent upkeep, newest first: ``(at, kind, ok, findings, detail)``."""
    return [
        (
            str(row[0]),
            str(row[1]),
            bool(row[2]),
            len(json.loads(row[3])),
            str(row[4]),
        )
        for row in conn.execute(
            "SELECT performed_at, kind, ok, findings_json, detail"
            " FROM db_maintenance ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    ]


def is_due(conn: sqlite3.Connection, kind: str, interval_hours: float) -> bool:
    """Has ``interval_hours`` passed since upkeep of ``kind`` last ran?"""
    last = last_performed(conn, kind)
    if last is None:
        return True
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        # An unparseable stamp is not a reason to never check again.
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return datetime.now(UTC) - when >= timedelta(hours=interval_hours)


def maybe_check(root: Path, config: object) -> HealthReport | None:
    """The automatic check: run at most once per configured interval.

    Returns None when it did not run — disabled, not due, or no database
    yet. Never raises: this is called before an unrelated command does
    its work, and a failing health check must not be what stops a scan.
    """
    if not getattr(config, "db_auto_check", True):
        return None

    path = db_path(root)
    if not path.is_file():
        return None

    try:
        status = get_status(root)
        if not status.up_to_date:
            # The database cannot be opened at all until it is migrated,
            # so this check cannot run and cannot record that it tried.
            # Reporting the one finding it *can* establish is still worth
            # more than silence, and it stops as soon as migrate is run.
            return HealthReport(
                at=datetime.now(UTC).isoformat(timespec="seconds"),
                size=DatabaseSize(
                    path=path,
                    file_bytes=_file_bytes(path),
                    wal_bytes=_file_bytes(path.parent / f"{path.name}-wal"),
                    page_size=0,
                    page_count=0,
                    freelist_count=0,
                    detailed=False,
                ),
                findings=[
                    Finding(
                        code="schema",
                        severity="warning",
                        message=(
                            f"Schema is at v{status.current_version}; this "
                            f"build expects v{status.target_version}. Your "
                            f"history is intact and untouched."
                        ),
                        remediation="vibe-sentinel migrate",
                    )
                ],
                schema_current=status.current_version,
                schema_target=status.target_version,
            )

        from vibe_sentinel.db.connection import get_db

        with get_db(root) as conn:
            interval = float(getattr(config, "db_check_interval_hours", 24.0))
            if not is_due(conn, "health", interval):
                return None
            report = check(conn, root, config, full=False)
            record(
                conn,
                "health",
                ok=report.ok,
                size=report.size,
                findings=report.findings,
                duration_ms=report.duration_ms,
            )
            return report
    except Exception as e:  # noqa: BLE001 - never break the real command
        logger.debug("automatic database check skipped: {}", e)
        return None


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def backup(root: Path, output: Path | None = None) -> BackupResult:
    """Copy the database while it may be in use.

    Uses SQLite's online backup API rather than ``shutil.copy``: a plain
    file copy taken mid-write yields a database whose pages come from two
    different transactions, and the WAL sidecar it needs to be consistent
    is a separate file that will not match. ``Connection.backup`` reads
    through the same locking every other reader uses, so the copy is a
    complete database at one point in time even while a scan writes.

    The copy is written under a temporary name and moved into place, so
    an interrupted backup never leaves a half-file that looks like one.
    """
    started = time.monotonic()
    source = db_path(root)
    if not source.is_file():
        raise FileNotFoundError(
            f"No history database at {source}. `vibe-sentinel scan` creates one."
        )

    if output is None:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        target = source.parent / BACKUP_DIR_NAME / f"{source.name}.backup.{stamp}"
    else:
        target = output
    target.parent.mkdir(parents=True, exist_ok=True)

    working = target.parent / f"{target.name}.partial"
    if working.exists():
        working.unlink()

    src = sqlite3.connect(str(source))
    try:
        dst = sqlite3.connect(str(working))
        try:
            src.backup(dst)
            # The copy owns no readers, so it can be compacted before it
            # is written out: a backup is read rarely and stored forever.
            dst.execute("VACUUM")
        finally:
            dst.close()
    finally:
        src.close()

    shutil.move(str(working), str(target))
    if output is None:
        # Only sweep the managed directory. An explicit --output is the
        # caller's own filing, and nothing here should tidy it.
        cleanup_old_backups(target.parent)
    result = BackupResult(
        path=target,
        size_bytes=_file_bytes(target),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    logger.info("database backed up to {} ({})", target, _human(result.size_bytes))
    return result


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------


def prune(
    conn: sqlite3.Connection,
    root: Path,
    *,
    cutoff: str,
    scans: bool = False,
    keep_runs: int = 10,
    apply: bool = False,
) -> PruneResult:
    """Delete records older than ``cutoff``. Counts only unless ``apply``.

    Scope is the journal by default — ``agent_commands`` and the actors
    left with nothing. That is operational volume: one row per tool call,
    thousands a session, and its value decays with age.

    ``scans=True`` extends it to the structural history, which is a
    different act entirely. That history cannot be re-measured — probes
    re-run against today's code, not March's — so it is guarded three
    ways: the baseline run is never deleted, the newest ``keep_runs`` are
    never deleted, and an applied prune takes a backup first, always.
    The backup is the revert; there is no undo.

    ``agent_sessions.command_count`` is then recomputed for every actor,
    not only the trimmed ones: the column is maintained by the writer, so
    the cheap way to know it agrees with the rows that survived is to
    count them. It afterwards means "commands still recorded", not
    "commands this actor ran" — the difference is what the
    ``db_maintenance`` row written by the caller records.
    """
    scope = ["journal"] + (["scans"] if scans else [])
    deleted: dict[str, int] = {}
    kept: list[int] = []

    stale_commands = _rows_before(conn, cutoff)
    deleted["agent_commands"] = stale_commands
    deleted["command_reviews"] = int(
        conn.execute(
            "SELECT COUNT(*) FROM command_reviews r JOIN agent_commands c"
            " ON c.id = r.command_id WHERE c.occurred_at < ?",
            (cutoff,),
        ).fetchone()[0]
    )
    deleted["agent_sessions"] = int(
        conn.execute(
            "SELECT COUNT(*) FROM agent_sessions s WHERE s.last_seen_at < ?"
            " AND NOT EXISTS (SELECT 1 FROM agent_commands c"
            " WHERE c.agent_session_id = s.id AND c.occurred_at >= ?)",
            (cutoff, cutoff),
        ).fetchone()[0]
    )

    doomed_runs: list[int] = []
    if scans:
        kept = [
            int(r[0])
            for r in conn.execute(
                "SELECT id FROM runs ORDER BY id DESC LIMIT ?", (max(keep_runs, 0),)
            )
        ]
        baseline = conn.execute("SELECT id FROM runs WHERE is_baseline = 1").fetchall()
        protected = set(kept) | {int(r[0]) for r in baseline}
        doomed_runs = [
            int(r[0])
            for r in conn.execute("SELECT id FROM runs WHERE started_at < ?", (cutoff,))
            if int(r[0]) not in protected
        ]
        deleted["runs"] = len(doomed_runs)
        # The only thing interpolated into the next two statements is a run of
        # ``?`` as long as ``doomed_runs`` — SQLite has no placeholder for a
        # variable-length IN list, so the list of placeholders is built and the
        # ids are still bound. The literal string never contains an id.
        for table in ("probe_runs", "observations", "changes", "gate_runs"):
            deleted[table] = (
                int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE run_id IN"  # nosec B608
                        f" ({','.join('?' * len(doomed_runs))})",
                        doomed_runs,
                    ).fetchone()[0]
                )
                if doomed_runs
                else 0
            )
        # Findings hang off `gate_runs`, not off `runs`, so they cannot be
        # counted by run_id like the three above. They go the same way —
        # the cascade reaches them through their gate run — and a count
        # that left them out would under-report what an --apply deletes.
        #
        # Only the gate runs a scan owns are counted, and only those are
        # deleted: `vibe-sentinel credentials` run on its own records with
        # a NULL run_id, which no cutoff here names, so prune leaves it.
        deleted["gate_findings"] = (
            int(
                conn.execute(
                    "SELECT COUNT(*) FROM gate_findings f JOIN gate_runs g"  # nosec B608
                    " ON g.id = f.gate_run_id WHERE g.run_id IN"
                    f" ({','.join('?' * len(doomed_runs))})",
                    doomed_runs,
                ).fetchone()[0]
            )
            if doomed_runs
            else 0
        )

    result = PruneResult(
        applied=False, cutoff=cutoff, scope=scope, deleted=deleted, kept_runs=kept
    )
    if not apply or result.total == 0:
        return result

    backup_result = backup(root)
    before_bytes = _file_bytes(db_path(root))

    conn.execute("BEGIN")
    try:
        # Commands first: the session sweep below asks which actors have
        # nothing left, and that is only true once these are gone.
        conn.execute("DELETE FROM agent_commands WHERE occurred_at < ?", (cutoff,))
        conn.execute(
            "DELETE FROM agent_sessions WHERE last_seen_at < ? AND NOT EXISTS"
            " (SELECT 1 FROM agent_commands c WHERE c.agent_session_id ="
            " agent_sessions.id)",
            (cutoff,),
        )
        conn.execute(
            "UPDATE agent_sessions SET command_count ="
            " (SELECT COUNT(*) FROM agent_commands c"
            "  WHERE c.agent_session_id = agent_sessions.id)"
        )
        if doomed_runs:
            conn.execute(
                f"DELETE FROM runs WHERE id IN ({','.join('?' * len(doomed_runs))})",  # nosec B608
                doomed_runs,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    result.applied = True
    result.backup_path = backup_result.path
    result.freed_bytes = max(before_bytes - _file_bytes(db_path(root)), 0)
    logger.info(
        "pruned {} row(s) older than {}; backup at {}",
        result.total,
        cutoff,
        backup_result.path,
    )
    return result


def cutoff_from_days(days: int) -> str:
    """The ISO timestamp ``days`` ago, as the record columns store it."""
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


def vacuum(root: Path) -> tuple[int, int]:
    """Rewrite the database compactly. Returns ``(before, after)`` bytes.

    Takes a backup first. VACUUM builds a new file and swaps it in, which
    is safe, but it is also the one routine operation that rewrites every
    page of the thing this tool exists to preserve.

    Runs ``ANALYZE`` afterwards. The planner picks between the indexes on
    ``agent_commands`` using ``sqlite_stat1``, and after a large prune
    those statistics describe a table that no longer exists.
    """
    path = db_path(root)
    backup(root)
    before = _file_bytes(path)

    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("ANALYZE")
        conn.commit()
    finally:
        conn.close()

    after = _file_bytes(path)
    logger.info("vacuumed {}: {} -> {}", path, _human(before), _human(after))
    return before, after


def reindex(root: Path) -> list[str]:
    """Recreate any declared index the database is missing.

    The migrations' ``CREATE INDEX IF NOT EXISTS`` statements are the
    definition of the index set, so re-running them is exactly what
    rebuilds it — there is no second copy of the DDL here to drift from
    the first. Returns the names that were created.
    """
    from vibe_sentinel.db.migration import _split_sql
    from vibe_sentinel.db.schema import MIGRATIONS

    path = db_path(root)
    conn = sqlite3.connect(str(path))
    try:
        before = _present_indexes(conn)
        if not head_indexes() - before:
            return []
        wanted = head_indexes()
        for version in sorted(MIGRATIONS):
            for statement in _split_sql(MIGRATIONS[version]):
                name = _created_index_name(_strip_comments(statement))
                # `name not in wanted` is the whole point: v1 still
                # contains `CREATE INDEX idx_changes_key`, and v5 exists
                # to have dropped it. Replaying every CREATE would undo
                # every migration that ever removed an index.
                if name is not None and name in wanted:
                    conn.execute(statement)
        conn.commit()
        return sorted(_present_indexes(conn) - before)
    finally:
        conn.close()


_CREATE_INDEX_RE = re.compile(
    r"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _created_index_name(statement: str) -> str | None:
    """The index a ``CREATE INDEX`` statement makes, or None for anything else."""
    match = _CREATE_INDEX_RE.match(statement)
    return match.group("name") if match else None


def _strip_comments(statement: str) -> str:
    """A statement with its leading ``--`` comment lines removed.

    ``_split_sql`` keeps the comments that introduce each statement, so
    the SQL verb is not necessarily the first word of the string.
    """
    lines = [
        line for line in statement.splitlines() if not line.strip().startswith("--")
    ]
    return "\n".join(lines).strip()


def _present_indexes(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }


# ---------------------------------------------------------------------------


def _human(size: float) -> str:
    """Bytes as something a person reads at a glance."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
