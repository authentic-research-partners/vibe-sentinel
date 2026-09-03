# The History File

Every record lands in one local SQLite file. It is the only artifact here that
**cannot be regenerated** — probes can be re-run, but only against the code as it
is now, never as it was in March. So it is never rebuilt: schema changes are
versioned migrations, and `get_db` refuses to open an out-of-date file rather
than migrating it silently.

```bash
vibe-sentinel db status     # what it weighs, per table and per index
vibe-sentinel db check      # what needs attention; exit 1 if anything does
vibe-sentinel db backup     # a copy, safe to take while a scan writes
vibe-sentinel db prune      # trim old records — counts first, deletes on --apply
vibe-sentinel db vacuum     # reclaim pages a prune freed
vibe-sentinel db reindex    # recreate a declared index that went missing
```

## It checks itself automatically, once a day

Any command that is not about the database itself runs a health check first, at most once per `check_interval_hours`. Three properties make this safe to leave switched on: findings go to **stderr** through the logger, so `scan
--format json` piped somewhere stays parseable; it **cannot raise or change the
exit code**, because a health check that breaks the scan somebody asked for is
one that gets turned off; and every finding **names the command that fixes it**.

It checks for integrity, missing indexes, free pages, an oversized write-ahead log, pending migrations, and any declared ceiling. Each run appends to
`db_maintenance`, so "when was this last looked at" travels with the file into a
backup or through a migration, rather than living in a stamp file beside it.

`db status` reports per-table and per-index bytes from SQLite's `dbstat`, which
turns "the database is big" into "the journal is big" — the one you can act on.

## Backing up

```bash
vibe-sentinel db backup                    # into .vibe-sentinel/backups/
vibe-sentinel db backup --output ~/hist.db
vibe-sentinel backups                      # every copy, however it was made
```

This uses SQLite's online backup API, not a file copy. A copy taken mid-write draws pages from two transactions and needs a WAL sidecar that will not match.
Backups are also taken automatically before a migration, an applied prune, and a
vacuum. **To restore, copy the file back over the database** — that is the whole
procedure.

## Trimming old records

```bash
vibe-sentinel db prune --older-than 90            # counts what it would delete
vibe-sentinel db prune --older-than 90 --apply    # does it, after backing up
```

`prune` is the only command here that deletes on purpose, so it has four safeguards:

| | |
|---|---|
| **Counts, doesn't delete** | until you add `--apply` |
| **Journal only** | the scan history needs `--scans` — *it* cannot be re-measured |
| **With `--scans`** | never the baseline run, never the newest `--keep-runs` (default 10) |
| **Always backs up first** | that backup is the revert; there is no undo |

The default scope is the command journal because that represents operational volume — one
row per tool call, thousands per session, value decaying with age. Deleting a run
takes its probe results, observations, changes and gate findings with it. A gate
you ran on its own belongs to no run, so a prune leaves it alone. Afterwards the
file has not shrunk; `db vacuum` rewrites it compactly.

## Configuration

Every `[database]` key is documented with commentary: use `vibe-sentinel scan --print-example`.
The ceilings default to **off** because a number hardcoded in a package cannot know whether 200 MB represents two years of healthy history or a runaway journal. The
backup warning is measured against the history at risk rather than against the
clock — a database created this morning has nothing a backup would have saved,
and a check that complains on somebody's first command is one they learn to
ignore.

Two write patterns share the file but make different durability tradeoffs: a scan
commits twice and keeps `synchronous=FULL`; the hook commits once per tool call
and uses `NORMAL` because an fsync costs about 7% of its budget, spent flushing a record of a command that has not run yet. Under WAL both are
corruption-safe; `NORMAL` gives up the last transactions to a machine power loss,
not to a process crash.

For adding a migration or an index, the checklists are in the module docstrings of
`vibe_sentinel/db/schema.py` and `vibe_sentinel/db/migration.py`.
