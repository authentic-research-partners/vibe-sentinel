"""Reading and writing runs, parameters, observations, and changes.

Every function here takes an open connection rather than opening its own.
The caller owns the ``get_db`` scope, so a whole scan writes inside one
connection and one transaction boundary instead of reopening the file per
table.
"""

from __future__ import annotations

import json
import sqlite3

from vibe_sentinel.schemas import (
    Change,
    DriftReport,
    Observation,
    ProbeResult,
    RunRecord,
    Snapshot,
    TrendPoint,
)

# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def save_run(
    conn: sqlite3.Connection,
    snapshot: Snapshot,
    report: DriftReport,
    baseline_run_id: int | None,
    make_baseline: bool,
) -> int:
    """Insert one scan — run, probes, observations, changes — atomically.

    Either the whole scan lands or none of it does. A run row without its
    observations would look like a scan that found nothing, which is
    indistinguishable from a codebase that genuinely emptied out.
    """
    conn.execute("BEGIN")
    try:
        cursor = conn.execute(
            "INSERT INTO runs (started_at, root, model, used_model,"
            " analyzed, is_baseline, probe_count, observation_count, change_count)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot.generated_at,
                snapshot.root,
                snapshot.model,
                int(snapshot.used_model),
                int(report.analyzed),
                0,  # set below, after any previous baseline is cleared
                len(snapshot.probes),
                sum(len(p.observations) for p in snapshot.probes.values()),
                len(report.changes),
            ),
        )
        run_id = int(cursor.lastrowid or 0)

        for probe_id, result in snapshot.probes.items():
            conn.execute(
                "INSERT INTO probe_runs (run_id, probe_id, title, command_json,"
                " parameters_json, ok, error, summary, duration_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    probe_id,
                    result.title,
                    json.dumps(result.command),
                    json.dumps(result.filled, sort_keys=True),
                    int(result.ok),
                    result.error,
                    result.summary,
                    result.duration_ms,
                ),
            )
            conn.executemany(
                "INSERT INTO observations (run_id, probe_id, key, value, label,"
                " attrs_json, risk) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        probe_id,
                        obs.key,
                        obs.value,
                        obs.label,
                        json.dumps(obs.attrs, sort_keys=True),
                        obs.risk,
                    )
                    for obs in result.observations
                ],
            )

        conn.executemany(
            "INSERT INTO changes (run_id, baseline_run_id, probe_id, key, kind,"
            " before_value, after_value, label, severity, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    baseline_run_id,
                    c.probe_id,
                    c.key,
                    c.kind,
                    c.before,
                    c.after,
                    c.label,
                    c.severity,
                    c.note,
                )
                for c in report.changes
            ],
        )

        if make_baseline:
            conn.execute("UPDATE runs SET is_baseline = 0 WHERE is_baseline = 1")
            conn.execute("UPDATE runs SET is_baseline = 1 WHERE id = ?", (run_id,))

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return run_id


def mark_baseline(conn: sqlite3.Connection, run_id: int) -> None:
    """Make ``run_id`` the run that later scans compare against."""
    conn.execute("BEGIN")
    try:
        conn.execute("UPDATE runs SET is_baseline = 0 WHERE is_baseline = 1")
        conn.execute("UPDATE runs SET is_baseline = 1 WHERE id = ?", (run_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _row_to_record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        id=row["id"],
        started_at=row["started_at"],
        root=row["root"],
        model=row["model"],
        used_model=bool(row["used_model"]),
        analyzed=bool(row["analyzed"]),
        is_baseline=bool(row["is_baseline"]),
        probe_count=row["probe_count"],
        observation_count=row["observation_count"],
        change_count=row["change_count"],
    )


def baseline_run(conn: sqlite3.Connection) -> RunRecord | None:
    """The run later scans compare against, or None before the first."""
    row = conn.execute(
        "SELECT * FROM runs WHERE is_baseline = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return _row_to_record(row) if row else None


def latest_run(conn: sqlite3.Connection) -> RunRecord | None:
    """The most recent run, baseline or not."""
    row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return _row_to_record(row) if row else None


def run_at_or_before(conn: sqlite3.Connection, at: str) -> RunRecord | None:
    """The newest run recorded no later than ``at``.

    The other end of a horizon comparison. Newest-at-or-before rather than
    nearest: a horizon says "at least this long ago", and a run three days
    inside it would report less movement than was asked for while still
    carrying the horizon's name.

    ``started_at`` rather than ``id`` because the question is about time,
    and the two only agree while nobody scans a second checkout of the
    same tree. It is also the indexed column, so this reads one row.
    """
    row = conn.execute(
        "SELECT * FROM runs WHERE started_at <= ?"
        " ORDER BY started_at DESC, id DESC LIMIT 1",
        (at,),
    ).fetchone()
    return _row_to_record(row) if row else None


def earliest_run(conn: sqlite3.Connection) -> RunRecord | None:
    """The oldest run still recorded — what bounds every horizon.

    Asked only to explain a horizon that found nothing to compare against.
    "No run from a month ago" and "the history starts three days ago" are
    the same fact, and only the second one tells you when to ask again.
    """
    row = conn.execute(
        "SELECT * FROM runs ORDER BY started_at ASC, id ASC LIMIT 1"
    ).fetchone()
    return _row_to_record(row) if row else None


def list_runs(conn: sqlite3.Connection, limit: int = 20) -> list[RunRecord]:
    """Recent runs, newest first."""
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_record(r) for r in rows]


def load_snapshot(conn: sqlite3.Connection, run_id: int) -> Snapshot | None:
    """Rebuild one run's snapshot from the database."""
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        return None

    snapshot = Snapshot(
        generated_at=run["started_at"],
        root=run["root"],
        model=run["model"],
        used_model=bool(run["used_model"]),
    )

    observations: dict[str, list[Observation]] = {}
    for row in conn.execute(
        "SELECT probe_id, key, value, label, attrs_json, risk FROM observations"
        " WHERE run_id = ? ORDER BY id",
        (run_id,),
    ):
        observations.setdefault(row["probe_id"], []).append(
            Observation(
                key=row["key"],
                value=row["value"],
                label=row["label"],
                attrs=json.loads(row["attrs_json"]),
                risk=row["risk"],
            )
        )

    for row in conn.execute(
        "SELECT probe_id, title, command_json, parameters_json, ok, error,"
        " summary, duration_ms FROM probe_runs WHERE run_id = ? ORDER BY id",
        (run_id,),
    ):
        probe_id = row["probe_id"]
        snapshot.probes[probe_id] = ProbeResult(
            probe_id=probe_id,
            title=row["title"],
            command=json.loads(row["command_json"]),
            filled=json.loads(row["parameters_json"]),
            observations=observations.get(probe_id, []),
            summary=row["summary"],
            ok=bool(row["ok"]),
            error=row["error"],
            duration_ms=row["duration_ms"],
        )
    return snapshot


def load_changes(conn: sqlite3.Connection, run_id: int) -> list[Change]:
    """The changes recorded for one run, as reported at the time."""
    return [
        Change(
            probe_id=row["probe_id"],
            key=row["key"],
            kind=row["kind"],
            before=row["before_value"],
            after=row["after_value"],
            label=row["label"],
            severity=row["severity"],
            note=row["note"],
        )
        for row in conn.execute(
            "SELECT probe_id, key, kind, before_value, after_value, label,"
            " severity, note FROM changes WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
    ]


def risks_at(conn: sqlite3.Connection, run_id: int) -> list[Observation]:
    """Observations that carried a provenance risk on one run.

    Reads the ``risk`` column directly rather than rebuilding the snapshot: the
    question is "what was wrong at run N", and the answer is a handful of rows
    out of the hundreds a run records.
    """
    return [
        Observation(
            key=row["key"],
            value=row["value"],
            label=row["label"],
            attrs=json.loads(row["attrs_json"]),
            risk=row["risk"],
        )
        for row in conn.execute(
            "SELECT key, value, label, attrs_json, risk FROM observations"
            " WHERE run_id = ? AND risk != '' ORDER BY risk, key",
            (run_id,),
        )
    ]


def risk_counts(conn: sqlite3.Connection, limit: int = 20) -> dict[int, dict[str, int]]:
    """Per-run tallies of each risk label, newest ``limit`` runs.

    One grouped query rather than one per run: the run listing shows this
    beside every row, and a per-row query would make listing history quadratic
    in the length of the history.
    """
    out: dict[int, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT run_id, risk, COUNT(*) AS n FROM observations"
        " WHERE risk != '' AND run_id IN"
        " (SELECT id FROM runs ORDER BY id DESC LIMIT ?)"
        " GROUP BY run_id, risk",
        (limit,),
    ):
        out.setdefault(int(row["run_id"]), {})[row["risk"]] = int(row["n"])
    return out


def trend(
    conn: sqlite3.Connection,
    probe_id: str,
    key: str,
    limit: int = 30,
) -> list[TrendPoint]:
    """One observation's value across runs, oldest first.

    This is what the history is for. A baseline comparison answers "did
    this move since last time"; a trend answers "has it been moving in one
    direction for weeks", which is how gradual structural drift actually
    presents — never as one alarming jump.
    """
    rows = conn.execute(
        "SELECT r.id AS run_id, r.started_at, o.value, o.label"
        " FROM observations o JOIN runs r ON r.id = o.run_id"
        " WHERE o.probe_id = ? AND o.key = ?"
        # o.run_id, not r.id: the join equates them, so the rows are the
        # same either way — but only this one is the third column of
        # idx_observations_trend, so only this one is sorted by the index
        # instead of by a temp B-tree built over the whole history.
        " ORDER BY o.run_id DESC LIMIT ?",
        (probe_id, key, limit),
    ).fetchall()
    return [
        TrendPoint(
            run_id=row["run_id"],
            at=row["started_at"],
            value=row["value"],
            label=row["label"],
        )
        for row in reversed(rows)
    ]


def series(
    conn: sqlite3.Connection,
    limit_runs: int = 50,
    min_runs: int = 10,
) -> dict[tuple[str, str], list[TrendPoint]]:
    """Every observation's recent history, keyed by ``(probe_id, key)``.

    One query for the whole set. The function this replaced —
    ``moving_keys`` — ran two more per key to fetch its endpoints, so a
    tree with three hundred observations asked six hundred questions to
    answer one, and it answered it by subtracting the first value from
    the last. That is not a trend: one refactor at either end decides it.
    :mod:`vibe_sentinel.trends` fits the whole series instead, and this
    is what hands it the series.

    ``limit_runs`` bounds both the cost and the meaning. The fit is
    quadratic in the length of a series, and a direction averaged over
    two years of history is not one anybody can act on — what is wanted
    is what the last fifty runs did.

    Series shorter than ``min_runs`` are dropped here rather than fitted
    and discarded later, because dropping them is the cheap half.
    """
    out: dict[tuple[str, str], list[TrendPoint]] = {}
    for row in conn.execute(
        "SELECT o.probe_id, o.key, o.run_id, r.started_at, o.value, o.label"
        " FROM observations o JOIN runs r ON r.id = o.run_id"
        " WHERE o.value IS NOT NULL AND o.run_id IN"
        " (SELECT id FROM runs ORDER BY id DESC LIMIT ?)"
        " ORDER BY o.probe_id, o.key, o.run_id",
        (limit_runs,),
    ):
        out.setdefault((row["probe_id"], row["key"]), []).append(
            TrendPoint(
                run_id=row["run_id"],
                at=row["started_at"],
                value=row["value"],
                label=row["label"],
            )
        )
    return {k: v for k, v in out.items() if len(v) >= min_runs}


def parameter_history(
    conn: sqlite3.Connection, probe_id: str, limit: int = 20
) -> list[tuple[int, str, str]]:
    """What the model chose for one probe's placeholders, per run.

    Returns ``(run_id, started_at, parameters_json)`` newest first. Worth
    checking when drift looks implausible: a model that picked a different
    SOURCE_ROOT this run manufactured the entire diff.
    """
    return [
        (row["id"], row["started_at"], row["parameters_json"])
        for row in conn.execute(
            "SELECT r.id, r.started_at, p.parameters_json"
            " FROM probe_runs p JOIN runs r ON r.id = p.run_id"
            # p.run_id for the same reason as trend's: it is the second
            # column of idx_probe_runs_probe_run, and r.id is not.
            " WHERE p.probe_id = ? ORDER BY p.run_id DESC LIMIT ?",
            (probe_id, limit),
        )
    ]
