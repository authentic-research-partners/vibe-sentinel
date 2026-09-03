"""Opening the history database.

Connections come from :func:`get_db` — a context manager that sets the
pragmas, yields, and closes. Never open a connection any other way; a
bare ``sqlite3.connect`` skips WAL and the busy timeout, and has no
cleanup guarantee.

A fresh database is created at the current schema version. An existing
database at an older version is **not** migrated silently — :func:`get_db`
raises :class:`SchemaMismatchError` naming the command that fixes it.
Silent migration means a routine command can rewrite a history file the
user has not backed up, and this history is the one thing here that
cannot be regenerated.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from vibe_sentinel.log import logger

from vibe_sentinel.db.schema import MIGRATIONS, SCHEMA_VERSION
from vibe_sentinel.exceptions import VibeSentinelError
from vibe_sentinel.paths import DB_FILENAME, SENTINEL_DIR

#: Location of the history database, relative to the project root.
DB_PATH = Path(SENTINEL_DIR) / DB_FILENAME

#: Wait this long for another process's write lock before giving up.
_BUSY_TIMEOUT_MS = 5000


class SchemaMismatchError(VibeSentinelError):
    """The database's schema version is not the one this build expects.

    A :class:`~vibe_sentinel.exceptions.VibeSentinelError` because it is
    re-exported from the package root and is the likeliest thing a
    Python-API consumer meets: ``except VibeSentinelError`` is the
    documented way to catch this library's failures, and for a while that
    line caught everything except the two that actually happen.
    """

    def __init__(self, current: int, target: int, db_path: Path) -> None:
        self.current = current
        self.target = target
        self.db_path = db_path
        if current > target:
            # Downgrading is not a migration — a newer build wrote this
            # file and may have used columns this build cannot see.
            # Migrating forward would be a no-op and dropping columns
            # would destroy history, so neither is offered.
            detail = (
                f"was written by a NEWER vibe-sentinel (schema v{current}); this "
                f"build expects v{target}. Upgrade vibe-sentinel, or point at a "
                f"different project root."
            )
        else:
            detail = (
                f"is at schema v{current}; this build expects v{target}. Your "
                f"run history is intact and untouched.\n"
                f"  Preview the update: vibe-sentinel migrate --dry-run\n"
                f"  Apply it:           vibe-sentinel migrate"
            )
        super().__init__(f"History database at {db_path} {detail}")


def db_path(root: Path) -> Path:
    """Path to the history database for the project at ``root``."""
    return root / DB_PATH


def read_schema_version(path: Path) -> int:
    """Return the database's schema version, or 0 when it has none.

    0 means the file has no ``schema_version`` table: either there is no
    file at all, or there is one written before versioning existed. Those
    are different situations and :func:`get_db` treats them differently —
    it creates a schema only when the file is absent, and refuses a file
    that exists and cannot say what it is. Rebuilding over an unreadable
    history would destroy the one artifact here that cannot be
    regenerated, on a guess about what it held.
    """
    if not path.is_file():
        return 0
    conn = sqlite3.connect(str(path), timeout=1)
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except sqlite3.OperationalError:
        logger.debug("schema_version table absent in {} — treating as v0", path)
        return 0
    finally:
        conn.close()


#: What ``synchronous`` costs, measured on this project: 400 journal
#: inserts took 1552 ms at FULL and 72 ms at NORMAL — 3.88 ms against
#: 0.18 ms each, for byte-identical files. The difference is entirely
#: fsync: FULL forces the write-ahead log to the platter on every commit,
#: NORMAL forces it at checkpoints and lets the OS schedule the rest.
#:
#: Under WAL both are safe from corruption; SQLite documents that. What
#: NORMAL gives up is the last few transactions if the *machine* loses
#: power. A process crash loses nothing either way, because the WAL is
#: still written — only the flush is deferred.
DURABILITY = "FULL"

#: For the PreToolUse hook, which commits once per tool call. Three
#: reasons it takes the other trade, and it needs all three:
#:
#:   - Volume. Thousands of commits a session against a handful for the
#:     scan history, so this is where every fsync actually lands.
#:   - Cost. 3.88 ms is ~7% of the hook's whole 52 ms budget, spent in
#:     front of every tool call the agent makes, to flush a record of a
#:     command that has not run yet.
#:   - What is at stake. `db prune` trims this journal *by default* —
#:     it is operational volume whose value decays — while it refuses to
#:     touch the scan history without --scans. Losing the last few
#:     entries to a power cut costs a fragment of a session that the
#:     power cut ended anyway.
#:
#: The scan history keeps FULL, and pays nothing for it: a whole scan is
#: two commits.
JOURNAL_DURABILITY = "NORMAL"


def _apply_pragmas(conn: sqlite3.Connection, synchronous: str = DURABILITY) -> None:
    """WAL for concurrent reads, FKs on, a busy timeout, and durability."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.execute(f"PRAGMA synchronous={synchronous}")
    conn.row_factory = sqlite3.Row


def init_db(path: Path) -> None:
    """Create a fresh database at the current schema version.

    Every migration is applied in order so a new file is identical to one
    that grew through the versions — the alternative, a separate "current
    schema" definition, is a second source of truth that drifts.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        _apply_pragmas(conn)
        for version in sorted(MIGRATIONS):
            conn.executescript(MIGRATIONS[version])
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        conn.commit()
        logger.info("history database created at v{}: {}", SCHEMA_VERSION, path)
    finally:
        conn.close()


@contextmanager
def get_db(root: Path, synchronous: str = DURABILITY) -> Iterator[sqlite3.Connection]:
    """Open the history database for ``root``, creating it if absent.

    Raises :class:`SchemaMismatchError` when the file exists at an older
    version — the caller is expected to surface the message, not migrate.

    ``synchronous`` is per-connection, not a property of the file, so the
    hook can take :data:`JOURNAL_DURABILITY` for its own writes while a
    scan of the same database keeps :data:`DURABILITY`. See those two for
    why they differ.
    """
    path = db_path(root)
    current = read_schema_version(path)

    if current == 0 and not path.is_file():
        init_db(path)
    elif current < SCHEMA_VERSION:
        raise SchemaMismatchError(current, SCHEMA_VERSION, path)
    elif current > SCHEMA_VERSION:
        raise SchemaMismatchError(current, SCHEMA_VERSION, path)

    conn = sqlite3.connect(str(path))
    try:
        _apply_pragmas(conn, synchronous)
        yield conn
    finally:
        conn.close()
