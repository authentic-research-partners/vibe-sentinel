"""Structural history storage.

The history database is the one artifact vibe-sentinel cannot regenerate:
probes can be re-run, but the shape the codebase had three months ago is
gone unless it was recorded. Everything here is built around not losing
it — versioned migrations rather than rebuilds, copy-on-write with a
backup, and no silent schema changes.

  - ``schema``      — SCHEMA_VERSION, MIGRATIONS, VERIFY, the index sets
  - ``connection``  — get_db(), init_db(), SchemaMismatchError
  - ``migration``   — copy-on-write engine and backup management
  - ``store``       — runs, parameters, observations, changes, trends
  - ``gate_store``  — gate runs and findings: what was *true*, per run,
    kept apart from ``store`` because a state is not a diff
  - ``journal_store`` — the agent command journal: actors and commands
  - ``maintenance`` — size, health, backup, prune, vacuum, reindex

Imports here are lazy for the same reason the top-level package's are: the
PreToolUse hook opens this database once per tool call, and eagerly
re-exporting the migration engine meant building its pydantic models to
insert one row. See :mod:`vibe_sentinel.journal`.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vibe_sentinel.db.connection import (
        DB_PATH,
        SchemaMismatchError,
        db_path,
        get_db,
        init_db,
        read_schema_version,
    )
    from vibe_sentinel.db.migration import (
        MigrationError,
        MigrationResult,
        MigrationStatus,
        get_status,
        list_backups,
        run_migration,
    )
    from vibe_sentinel.db.maintenance import (
        BackupResult,
        DatabaseSize,
        Finding,
        HealthReport,
        PruneResult,
        backup,
        check,
        maybe_check,
        measure,
        prune,
        reindex,
        vacuum,
    )
    from vibe_sentinel.db.schema import SCHEMA_VERSION

_LAZY: dict[str, str] = {
    "BackupResult": "vibe_sentinel.db.maintenance",
    "DB_PATH": "vibe_sentinel.db.connection",
    "DatabaseSize": "vibe_sentinel.db.maintenance",
    "Finding": "vibe_sentinel.db.maintenance",
    "HealthReport": "vibe_sentinel.db.maintenance",
    "MigrationError": "vibe_sentinel.db.migration",
    "MigrationResult": "vibe_sentinel.db.migration",
    "MigrationStatus": "vibe_sentinel.db.migration",
    "PruneResult": "vibe_sentinel.db.maintenance",
    "SCHEMA_VERSION": "vibe_sentinel.db.schema",
    "SchemaMismatchError": "vibe_sentinel.db.connection",
    "backup": "vibe_sentinel.db.maintenance",
    "check": "vibe_sentinel.db.maintenance",
    "db_path": "vibe_sentinel.db.connection",
    "get_db": "vibe_sentinel.db.connection",
    "get_status": "vibe_sentinel.db.migration",
    "init_db": "vibe_sentinel.db.connection",
    "list_backups": "vibe_sentinel.db.migration",
    "maybe_check": "vibe_sentinel.db.maintenance",
    "measure": "vibe_sentinel.db.maintenance",
    "prune": "vibe_sentinel.db.maintenance",
    "read_schema_version": "vibe_sentinel.db.connection",
    "reindex": "vibe_sentinel.db.maintenance",
    "run_migration": "vibe_sentinel.db.migration",
    "vacuum": "vibe_sentinel.db.maintenance",
}


def __getattr__(name: str) -> Any:
    try:
        module_name = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "DB_PATH",
    "SCHEMA_VERSION",
    "BackupResult",
    "DatabaseSize",
    "Finding",
    "HealthReport",
    "MigrationError",
    "MigrationResult",
    "MigrationStatus",
    "PruneResult",
    "SchemaMismatchError",
    "backup",
    "check",
    "db_path",
    "get_db",
    "get_status",
    "init_db",
    "list_backups",
    "maybe_check",
    "measure",
    "prune",
    "read_schema_version",
    "reindex",
    "run_migration",
    "vacuum",
]
