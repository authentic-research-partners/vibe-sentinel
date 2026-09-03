"""Reading and writing what the gates found.

Separate from :mod:`vibe_sentinel.db.store` for the reason the tables are
separate: that module records what *moved*, and these rows record what
*is*. Mixing them would put a standing fact behind a diff, which is
exactly the shape that let a committed key sit in a baseline unmentioned.

Like ``store``, every function takes an open connection. The caller owns
the ``get_db`` scope, so a scan's probes and its gates land inside one
connection.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from vibe_sentinel.schemas import GateFinding, GateReport, GateState

# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def save_gate_state(
    conn: sqlite3.Connection,
    state: GateState,
    root: str,
    run_id: int | None = None,
) -> list[int]:
    """Record every gate's findings, atomically.

    ``run_id`` ties these to a scan; ``None`` means the gate was run on
    its own. Either the whole state lands or none of it does — a
    ``gate_runs`` row without its findings would read as a gate that
    completed and found nothing, which is the one wrong answer here.

    Returns the ``gate_runs`` ids, in report order.
    """
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    conn.execute("BEGIN")
    try:
        ids: list[int] = []
        for report in state.reports:
            cursor = conn.execute(
                "INSERT INTO gate_runs (run_id, gate, started_at, root, ok,"
                " configured, adjudicated, finding_count, failing_count, summary,"
                " error, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    report.gate,
                    started_at,
                    root,
                    int(report.ok),
                    int(report.configured),
                    int(report.adjudicated),
                    len(report.findings),
                    len(report.failing),
                    report.summary,
                    report.error,
                    report.duration_ms,
                ),
            )
            gate_run_id = int(cursor.lastrowid or 0)
            ids.append(gate_run_id)
            conn.executemany(
                "INSERT INTO gate_findings (gate_run_id, gate, key, kind, subject,"
                " label, detail, risk, verdict, failing, pinned, adjudicated,"
                " reason, attrs_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        gate_run_id,
                        finding.gate,
                        finding.key,
                        finding.kind,
                        finding.subject,
                        finding.label,
                        finding.detail,
                        finding.risk,
                        finding.verdict,
                        int(finding.failing),
                        int(finding.pinned),
                        int(finding.adjudicated),
                        finding.reason,
                        json.dumps(finding.attrs, sort_keys=True),
                    )
                    for finding in report.findings
                ],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return ids


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _to_finding(row: sqlite3.Row) -> GateFinding:
    return GateFinding(
        gate=row["gate"],
        key=row["key"],
        kind=row["kind"],
        subject=row["subject"],
        label=row["label"],
        detail=row["detail"],
        risk=row["risk"],
        verdict=row["verdict"],
        failing=bool(row["failing"]),
        pinned=bool(row["pinned"]),
        adjudicated=bool(row["adjudicated"]),
        reason=row["reason"],
        attrs=json.loads(row["attrs_json"]),
    )


def findings_at(conn: sqlite3.Connection, gate_run_id: int) -> list[GateFinding]:
    """One gate run's findings, as recorded at the time."""
    return [
        _to_finding(row)
        for row in conn.execute(
            "SELECT * FROM gate_findings WHERE gate_run_id = ?"
            " ORDER BY failing DESC, gate, key",
            (gate_run_id,),
        )
    ]


def state_at(conn: sqlite3.Connection, run_id: int) -> GateState:
    """Every gate's result for one scan, rebuilt as it was reported."""
    reports: list[GateReport] = []
    for row in conn.execute(
        "SELECT * FROM gate_runs WHERE run_id = ? ORDER BY id", (run_id,)
    ):
        reports.append(
            GateReport(
                gate=row["gate"],
                ok=bool(row["ok"]),
                configured=bool(row["configured"]),
                adjudicated=bool(row["adjudicated"]),
                findings=tuple(findings_at(conn, int(row["id"]))),
                summary=row["summary"],
                error=row["error"],
                duration_ms=int(row["duration_ms"]),
            )
        )
    return GateState(reports=tuple(reports))


def first_seen(conn: sqlite3.Connection, gate: str, key: str) -> tuple[str, int] | None:
    """When this finding was first recorded, and how many times since.

    The question a gate could not answer before these tables existed:
    "when did this start". A standing finding reports identically on
    every run, so the only way to date it is the record.
    """
    row = conn.execute(
        "SELECT MIN(r.started_at) AS first_at, COUNT(*) AS seen"
        " FROM gate_findings f JOIN gate_runs r ON r.id = f.gate_run_id"
        " WHERE f.gate = ? AND f.key = ?",
        (gate, key),
    ).fetchone()
    if row is None or row["first_at"] is None:
        return None
    return (str(row["first_at"]), int(row["seen"]))


def latest_gate_run(conn: sqlite3.Connection, gate: str) -> int | None:
    """The newest run of one gate, however it was invoked."""
    row = conn.execute(
        "SELECT id FROM gate_runs WHERE gate = ? ORDER BY id DESC LIMIT 1",
        (gate,),
    ).fetchone()
    return int(row["id"]) if row else None
