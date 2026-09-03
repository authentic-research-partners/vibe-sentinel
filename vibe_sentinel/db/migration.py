"""Copy-on-write migration engine (Sqitch-inspired, SQLite-adapted).

Migrations never touch the original file. The database is copied, the
copy is migrated and verified, and only then does it take the original's
place — the original becoming a timestamped backup. If anything fails at
any point, the copy is deleted and the original has not been opened for
writing at all.

That matters more here than in most tools: the history database is the
one artifact vibe-sentinel cannot regenerate. Losing it doesn't cost a
cache, it costs every past run you would have compared against.

Four rules, from the model this follows:

1. **Never migrate silently.** The user runs ``vibe-sentinel migrate``.
2. **Copy on write.** The original is untouched until the swap.
3. **Deploy and verify.** Each migration proves it landed before the next
   one runs.
4. **No revert scripts.** The backup is the revert — copy the file back.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger
from pydantic import BaseModel

from vibe_sentinel.db.connection import db_path, read_schema_version
from vibe_sentinel.exceptions import VibeSentinelError
from vibe_sentinel.db.schema import (
    ABSENT_INDEXES,
    EXPECT_INDEXES,
    FK_OFF_MIGRATIONS,
    MIGRATIONS,
    SCHEMA_VERSION,
    VERIFY,
)

BACKUP_DIR_NAME = "backups"
BACKUP_MAX_AGE_DAYS = 30
#: Two shapes of backup file. A migration leaves `<db>.pre-v5.<ts>`; an
#: explicit `vibe-sentinel db backup` leaves `<db>.backup.<ts>`. Both are
#: complete databases and either can be copied back, but only one of them
#: was somebody's deliberate act, and retention treats them differently.
_BACKUP_FILENAME_RE = re.compile(
    r"^(?P<stem>.+?)\.(?:pre-v(?P<version>\d+)|(?P<manual>backup))"
    r"\.(?P<ts>\d{8}-\d{6})$"
)


class MigrationError(VibeSentinelError):
    """Raised when a migration cannot be applied or does not verify.

    In the :class:`~vibe_sentinel.exceptions.VibeSentinelError` hierarchy
    for the same reason :class:`~vibe_sentinel.db.connection.SchemaMismatchError`
    is: it is what a Python-API consumer meets when the file on disk is not
    the one this build expects, and ``except VibeSentinelError`` is the
    documented way to catch that.
    """


class MigrationStatus(BaseModel):
    """Where the database stands relative to this build."""

    current_version: int
    target_version: int
    pending: list[int]
    db_path: Path
    exists: bool

    @property
    def up_to_date(self) -> bool:
        return not self.pending


class MigrationResult(BaseModel):
    """Outcome of one migration run."""

    success: bool
    from_version: int
    to_version: int
    backup_path: Path | None = None
    error: str = ""


class BackupInfo(BaseModel):
    """A copy of the database, from a migration or taken deliberately."""

    path: Path
    size_bytes: int
    created_at: str
    pre_version: int | None = None
    #: ``migration``, ``manual``, or ``unknown`` for a file someone put
    #: in the directory by hand.
    kind: str = "unknown"


def get_status(root: Path) -> MigrationStatus:
    """Read migration status without modifying anything."""
    path = db_path(root)
    current = read_schema_version(path)
    pending = [v for v in sorted(MIGRATIONS) if current < v <= SCHEMA_VERSION]
    return MigrationStatus(
        current_version=current,
        target_version=SCHEMA_VERSION,
        pending=pending,
        db_path=path,
        exists=path.is_file(),
    )


def _check_not_locked(path: Path) -> None:
    """Fail early when another process holds the database.

    Migrating a file someone else is writing to would either block for
    the busy timeout or interleave with their transaction.
    """
    conn = sqlite3.connect(str(path), timeout=1)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError as e:
        raise MigrationError(
            f"History database at {path} is locked — another vibe-sentinel "
            f"is running. Wait for it to finish, then retry."
        ) from e
    finally:
        conn.close()


def _split_sql(sql: str) -> list[str]:
    """Split multi-statement migration SQL into single statements.

    ``executescript`` is avoided deliberately: it commits after each
    statement, so a migration that fails halfway leaves the database in a
    partial state with nothing to roll back to. Splitting lets every
    migration run inside one explicit transaction.

    Splits on a semicolon at end of line, which is correct for all
    migration SQL here (no semicolons inside string literals).
    """
    statements: list[str] = []
    for part in sql.split(";\n"):
        cleaned = part.strip()
        if not cleaned:
            continue
        body = [
            line
            for line in cleaned.splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]
        if not body:
            continue
        statements.append(cleaned.rstrip(";") + ";")
    return statements


def _apply_one(conn: sqlite3.Connection, version: int) -> None:
    """Apply one migration inside a single transaction."""
    statements = _split_sql(MIGRATIONS[version])
    conn.execute("BEGIN")
    try:
        for stmt in statements:
            conn.execute(stmt)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _index_names(conn: sqlite3.Connection) -> set[str]:
    """Every index name in the database, however it was created."""
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }


def _verify_indexes(conn: sqlite3.Connection, version: int) -> None:
    """Check the index names a migration declares it created and dropped.

    Declared rather than queried because a query cannot check this. A
    ``VERIFY`` entry proves its SQL is *valid*, which is exactly right for a
    column — ``SELECT col FROM t LIMIT 0`` will not prepare if ``col`` is
    gone. It proves nothing about an index: ``SELECT 1 FROM sqlite_master
    WHERE name='idx_x'`` prepares whether or not ``idx_x`` exists, and
    returning no rows is not an error. Every index assertion written that way
    passes unconditionally.
    """
    present = _index_names(conn)

    missing = [n for n in EXPECT_INDEXES.get(version, []) if n not in present]
    if missing:
        raise MigrationError(
            f"Verify failed for v{version}: index(es) not created: {', '.join(missing)}"
        )

    survived = [n for n in ABSENT_INDEXES.get(version, []) if n in present]
    if survived:
        raise MigrationError(
            f"Verify failed for v{version}: index(es) that should have been "
            f"dropped are still present: {', '.join(survived)}"
        )


def _verify_one(conn: sqlite3.Connection, version: int) -> None:
    """Run the verify queries for one migration; raise on any failure."""
    queries = VERIFY.get(version, [])
    if not queries:
        raise MigrationError(
            f"Migration v{version} declares no verify queries. Every "
            f"structural change needs at least one — add them to VERIFY in "
            f"db/schema.py."
        )
    for i, query in enumerate(queries, start=1):
        try:
            # fetchall, not execute: execute only prepares the statement, so
            # anything that fails while producing rows would go unnoticed.
            conn.execute(query).fetchall()
        except sqlite3.Error as e:
            raise MigrationError(
                f"Verify failed for v{version}, query {i}: {e}\n  SQL: {query}"
            ) from e

    _verify_indexes(conn, version)


def _cleanup(working_copy: Path) -> None:
    """Delete a working copy and its WAL sidecars."""
    for suffix in ("", "-wal", "-shm"):
        candidate = working_copy.parent / f"{working_copy.name}{suffix}"
        if candidate.exists():
            candidate.unlink()


def run_migration(root: Path) -> MigrationResult:
    """Migrate the history database to the current schema version."""
    status = get_status(root)
    path = status.db_path

    if not status.exists:
        raise MigrationError(
            f"No history database at {path}. Nothing to migrate — "
            f"`vibe-sentinel scan` creates one at the current version."
        )
    if status.up_to_date:
        return MigrationResult(
            success=True,
            from_version=status.current_version,
            to_version=status.current_version,
        )
    if status.current_version > SCHEMA_VERSION:
        raise MigrationError(
            f"History database at {path} is at v{status.current_version}, newer "
            f"than this build's v{SCHEMA_VERSION}. Upgrade vibe-sentinel."
        )

    _check_not_locked(path)

    working_copy = path.parent / f"{path.name}.migrating"
    if working_copy.exists():
        logger.warning("removing stale working copy from an interrupted migration")
        _cleanup(working_copy)

    # Flush the WAL so the copy is a complete database on its own.
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    shutil.copy2(path, working_copy)

    try:
        conn = sqlite3.connect(str(working_copy))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            for version in status.pending:
                fk_off = version in FK_OFF_MIGRATIONS
                if fk_off:
                    # Must be set outside a transaction; SQLite enforces
                    # FKs during DROP TABLE and would cascade the delete
                    # into child rows during a table rebuild.
                    conn.execute("PRAGMA foreign_keys=OFF")

                logger.info("applying migration v{}", version)
                _apply_one(conn, version)

                if fk_off:
                    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                    if violations:
                        raise MigrationError(
                            f"FK integrity violation after v{version}: {violations}"
                        )
                    conn.execute("PRAGMA foreign_keys=ON")

                _verify_one(conn, version)
                logger.info("verified migration v{}", version)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except Exception as e:
        _cleanup(working_copy)
        logger.error("migration failed, original untouched: {}", e)
        return MigrationResult(
            success=False,
            from_version=status.current_version,
            to_version=SCHEMA_VERSION,
            error=str(e),
        )

    backups_dir = path.parent / BACKUP_DIR_NAME
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup_path = backups_dir / f"{path.name}.pre-v{SCHEMA_VERSION}.{timestamp}"

    shutil.move(str(path), str(backup_path))
    shutil.move(str(working_copy), str(path))
    for suffix in ("-wal", "-shm"):
        leftover = path.parent / f"{path.name}{suffix}"
        if leftover.exists():
            leftover.unlink()

    removed = cleanup_old_backups(backups_dir)
    if removed:
        logger.info(
            "removed {} backup(s) older than {} days", len(removed), BACKUP_MAX_AGE_DAYS
        )

    logger.info(
        "migrated v{} -> v{}; previous database kept at {}",
        status.current_version,
        SCHEMA_VERSION,
        backup_path,
    )
    return MigrationResult(
        success=True,
        from_version=status.current_version,
        to_version=SCHEMA_VERSION,
        backup_path=backup_path,
    )


def list_backups(backups_dir: Path) -> list[BackupInfo]:
    """Every backup in ``backups_dir``, newest first."""
    if not backups_dir.is_dir():
        return []
    out: list[BackupInfo] = []
    for path in backups_dir.iterdir():
        if not path.is_file():
            continue
        match = _BACKUP_FILENAME_RE.match(path.name)
        stat = path.stat()
        version = match.group("version") if match else None
        if match is None:
            kind = "unknown"
        elif match.group("manual"):
            kind = "manual"
        else:
            kind = "migration"
        out.append(
            BackupInfo(
                path=path,
                size_bytes=stat.st_size,
                created_at=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(
                    timespec="seconds"
                ),
                pre_version=int(version) if version else None,
                kind=kind,
            )
        )
    return sorted(out, key=lambda b: b.created_at, reverse=True)


def cleanup_old_backups(backups_dir: Path) -> list[Path]:
    """Delete backups older than :data:`BACKUP_MAX_AGE_DAYS`.

    The most recent backup **of each kind** is always kept regardless of
    age. A project scanned twice a year would otherwise migrate and
    immediately delete the only copy of its previous history; and a
    backup someone took deliberately should not evaporate because a
    migration happened to run afterwards and left a newer one.

    A file whose name matches neither shape is never touched. Somebody
    put it there on purpose and this function does not know what it is.
    """
    cutoff = datetime.now(UTC) - timedelta(days=BACKUP_MAX_AGE_DAYS)
    removed: list[Path] = []
    seen: set[str] = set()
    for backup in list_backups(backups_dir):  # newest first
        if backup.kind == "unknown":
            continue
        if backup.kind not in seen:
            seen.add(backup.kind)
            continue
        if datetime.fromisoformat(backup.created_at) < cutoff:
            backup.path.unlink()
            removed.append(backup.path)
    return removed
