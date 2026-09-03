"""Persisting and reading runs, parameters, observations, and changes."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibe_sentinel.db import store
from vibe_sentinel.db.connection import db_path, get_db, init_db
from vibe_sentinel.schemas import (
    Change,
    DriftReport,
    Observation,
    ProbeResult,
    Snapshot,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    init_db(db_path(tmp_path))
    return tmp_path


def _snapshot(
    observations: list[tuple[str, float]],
    probe_id: str = "p",
    params: dict[str, str] | None = None,
) -> Snapshot:
    return Snapshot(
        root="src",
        model="qwen3",
        thinking="low",
        probes={
            probe_id: ProbeResult(
                probe_id=probe_id,
                title="A probe",
                command=["echo", "hi"],
                filled=params or {"ROOT": "src"},
                summary="a summary",
                duration_ms=12,
                observations=[
                    Observation(key=k, value=v, label=f"{k} is {v}", attrs={"n": "1"})
                    for k, v in observations
                ],
            )
        },
    )


def _save(project: Path, snapshot: Snapshot, report: DriftReport, baseline=True) -> int:
    with get_db(project) as conn:
        return store.save_run(conn, snapshot, report, None, make_baseline=baseline)


# --- writing ---------------------------------------------------------------


def test_run_round_trips(project: Path) -> None:
    run_id = _save(project, _snapshot([("a", 1.0)]), DriftReport(first_run=True))
    with get_db(project) as conn:
        loaded = store.load_snapshot(conn, run_id)
    assert loaded is not None
    assert loaded.model == "qwen3"
    result = loaded.probes["p"]
    assert result.title == "A probe"
    assert result.command == ["echo", "hi"]
    assert result.summary == "a summary"
    assert result.duration_ms == 12
    assert result.by_key()["a"].value == 1.0
    assert result.by_key()["a"].attrs == {"n": "1"}


def test_parameters_are_recorded(project: Path) -> None:
    """Without this, a model that silently changed SOURCE_ROOT between
    runs would manufacture drift invisibly."""
    _save(
        project,
        _snapshot([("a", 1.0)], params={"ROOT": "src", "GLOB": "*.py"}),
        DriftReport(first_run=True),
    )
    with get_db(project) as conn:
        history = store.parameter_history(conn, "p")
    assert len(history) == 1
    assert '"ROOT": "src"' in history[0][2]
    assert '"GLOB": "*.py"' in history[0][2]


def test_changes_are_recorded_as_reported(project: Path) -> None:
    """A later model version rates differently; what was actually said at
    the time should not move."""
    report = DriftReport(
        changes=[
            Change(
                probe_id="p",
                key="dir:src/helpers",
                kind="appeared",
                after=3.0,
                label="new dir",
                severity="high",
                note="a directory that did not exist",
            )
        ]
    )
    run_id = _save(project, _snapshot([("a", 1.0)]), report)
    with get_db(project) as conn:
        changes = store.load_changes(conn, run_id)
    assert len(changes) == 1
    assert changes[0].severity == "high"
    assert changes[0].note == "a directory that did not exist"


def test_save_is_atomic(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run row without its observations would read as a scan that found
    nothing — indistinguishable from a codebase that emptied out."""
    import json as _json

    calls = {"n": 0}
    real_dumps = _json.dumps

    def exploding_dumps(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("boom")
        return real_dumps(*args, **kwargs)

    monkeypatch.setattr(store.json, "dumps", exploding_dumps)
    with pytest.raises(RuntimeError, match="boom"), get_db(project) as conn:
        store.save_run(conn, _snapshot([("a", 1.0)]), DriftReport(), None, True)

    with get_db(project) as conn:
        assert store.list_runs(conn) == []


# --- baseline --------------------------------------------------------------


def test_first_run_becomes_baseline(project: Path) -> None:
    run_id = _save(project, _snapshot([("a", 1.0)]), DriftReport(first_run=True))
    with get_db(project) as conn:
        baseline = store.baseline_run(conn)
    assert baseline is not None
    assert baseline.id == run_id


def test_run_without_update_does_not_become_baseline(project: Path) -> None:
    """A scan that finds drift must not quietly become the thing later
    scans compare against."""
    first = _save(project, _snapshot([("a", 1.0)]), DriftReport(first_run=True))
    _save(project, _snapshot([("a", 9.0)]), DriftReport(), baseline=False)
    with get_db(project) as conn:
        baseline = store.baseline_run(conn)
        assert baseline is not None
        assert baseline.id == first
        assert len(store.list_runs(conn)) == 2


def test_exactly_one_baseline_after_marking(project: Path) -> None:
    _save(project, _snapshot([("a", 1.0)]), DriftReport(first_run=True))
    second = _save(project, _snapshot([("a", 2.0)]), DriftReport(), baseline=True)
    with get_db(project) as conn:
        rows = conn.execute("SELECT id FROM runs WHERE is_baseline = 1").fetchall()
        assert [r["id"] for r in rows] == [second]


def test_mark_baseline_moves_it(project: Path) -> None:
    first = _save(project, _snapshot([("a", 1.0)]), DriftReport(first_run=True))
    _save(project, _snapshot([("a", 2.0)]), DriftReport(), baseline=True)
    with get_db(project) as conn:
        store.mark_baseline(conn, first)
        baseline = store.baseline_run(conn)
        assert baseline is not None
        assert baseline.id == first


def test_no_baseline_before_any_run(project: Path) -> None:
    with get_db(project) as conn:
        assert store.baseline_run(conn) is None
        assert store.latest_run(conn) is None


# --- trends ----------------------------------------------------------------


def test_trend_returns_values_oldest_first(project: Path) -> None:
    for value in (1.0, 2.0, 5.0):
        _save(project, _snapshot([("dir:x", value)]), DriftReport(), baseline=False)
    with get_db(project) as conn:
        points = store.trend(conn, "p", "dir:x")
    assert [p.value for p in points] == [1.0, 2.0, 5.0]


def test_trend_of_unknown_key_is_empty(project: Path) -> None:
    _save(project, _snapshot([("a", 1.0)]), DriftReport())
    with get_db(project) as conn:
        assert store.trend(conn, "p", "nope") == []


def test_series_returns_each_observation_s_history_in_one_query(
    project: Path,
) -> None:
    """The function this replaced ran two more queries per key to fetch
    its endpoints — six hundred questions on a tree with three hundred
    observations — and answered "did it move" by subtracting one from the
    other, which one refactor at either end decides."""
    for value in (2.0, 3.0, 5.0):
        _save(
            project,
            _snapshot([("dir:helpers", value), ("dir:stable", 4.0)]),
            DriftReport(),
            baseline=False,
        )
    with get_db(project) as conn:
        recorded = store.series(conn, min_runs=3)

    assert set(recorded) == {("p", "dir:helpers"), ("p", "dir:stable")}
    assert [pt.value for pt in recorded[("p", "dir:helpers")]] == [2.0, 3.0, 5.0]
    # Oldest first: a fit reads the series forwards, and reversing it
    # would reverse every slope in the report.
    assert [pt.run_id for pt in recorded[("p", "dir:helpers")]] == [1, 2, 3]


def test_series_keeps_the_key_that_did_not_move(project: Path) -> None:
    """Unlike its predecessor, which dropped them. A series that has held
    one value for thirty runs is the baseline an anomaly is measured
    against, and it is exactly the series where a jump matters most."""
    for _ in range(3):
        _save(project, _snapshot([("dir:stable", 4.0)]), DriftReport(), baseline=False)
    with get_db(project) as conn:
        assert ("p", "dir:stable") in store.series(conn, min_runs=3)


def test_series_drops_what_is_too_short_to_fit(project: Path) -> None:
    """Cheaper here than fitting it and discarding the result."""
    for value in (2.0, 3.0):
        _save(project, _snapshot([("dir:a", value)]), DriftReport(), baseline=False)
    with get_db(project) as conn:
        assert store.series(conn, min_runs=3) == {}
        assert len(store.series(conn, min_runs=2)) == 1


def test_series_looks_back_only_as_far_as_asked(project: Path) -> None:
    """Bounds the cost, which is quadratic in the length of a series, and
    the meaning: a direction averaged over two years is not one anybody
    can act on."""
    for value in (1.0, 2.0, 3.0, 4.0, 5.0):
        _save(project, _snapshot([("dir:a", value)]), DriftReport(), baseline=False)
    with get_db(project) as conn:
        recent = store.series(conn, limit_runs=2, min_runs=1)
    assert [pt.value for pt in recent[("p", "dir:a")]] == [4.0, 5.0]


def test_load_snapshot_of_unknown_run_is_none(project: Path) -> None:
    with get_db(project) as conn:
        assert store.load_snapshot(conn, 999) is None


def test_failed_probe_round_trips_with_its_error(project: Path) -> None:
    snapshot = Snapshot(
        probes={"p": ProbeResult(probe_id="p", ok=False, error="boom", title="t")}
    )
    run_id = _save(project, snapshot, DriftReport(first_run=True))
    with get_db(project) as conn:
        loaded = store.load_snapshot(conn, run_id)
    assert loaded is not None
    assert loaded.probes["p"].ok is False
    assert loaded.probes["p"].error == "boom"


# --- provenance risk (schema v3) -------------------------------------------


def _risky_snapshot(rows: list[tuple[str, str]], probe_id: str = "p") -> Snapshot:
    """A snapshot whose observations carry provenance risks."""
    return Snapshot(
        root="src",
        model="qwen3",
        probes={
            probe_id: ProbeResult(
                probe_id=probe_id,
                title="A probe",
                command=["echo", "hi"],
                observations=[
                    Observation(key=key, label=key, risk=risk) for key, risk in rows
                ],
            )
        },
    )


def test_risk_round_trips(project: Path) -> None:
    """The column is written and read back verbatim. A probe's measurement is
    not the model's to edit, so nothing between here and the report may
    normalise, rank or reinterpret it."""
    run_id = _save(
        project,
        _risky_snapshot([("package:openai", "orphan"), ("package:httpx", "")]),
        DriftReport(),
    )
    with get_db(project) as conn:
        snapshot = store.load_snapshot(conn, run_id)
    assert snapshot is not None
    risks = {o.key: o.risk for o in snapshot.probes["p"].observations}
    assert risks == {"package:openai": "orphan", "package:httpx": ""}


def test_risks_at_returns_only_the_flagged_observations(project: Path) -> None:
    run_id = _save(
        project,
        _risky_snapshot(
            [
                ("import:invented", "unregistered"),
                ("package:openai", "orphan"),
                ("package:httpx", ""),
            ]
        ),
        DriftReport(),
    )
    with get_db(project) as conn:
        flagged = store.risks_at(conn, run_id)
    assert [(o.key, o.risk) for o in flagged] == [
        ("package:openai", "orphan"),
        ("import:invented", "unregistered"),
    ]


def test_risk_counts_are_grouped_per_run(project: Path) -> None:
    """The run listing shows this beside every row, so it has to come back in
    one query rather than one per run."""
    first = _save(
        project,
        _risky_snapshot([("a", "orphan"), ("b", "orphan"), ("c", "phantom")]),
        DriftReport(),
    )
    second = _save(
        project, _risky_snapshot([("a", "orphan")]), DriftReport(), baseline=False
    )
    with get_db(project) as conn:
        counts = store.risk_counts(conn)
    assert counts[first] == {"orphan": 2, "phantom": 1}
    assert counts[second] == {"orphan": 1}


def test_a_run_with_no_risks_has_no_rows_in_the_index(project: Path) -> None:
    run_id = _save(project, _snapshot([("k", 1.0)]), DriftReport())
    with get_db(project) as conn:
        assert store.risks_at(conn, run_id) == []
        assert store.risk_counts(conn) == {}


# --- the other end of a horizon comparison ----------------------------------


def _at(project: Path, when: str) -> int:
    """Record a run stamped at ``when``."""
    snapshot = _snapshot([("a", 1.0)])
    snapshot.generated_at = when
    return _save(project, snapshot, DriftReport(), baseline=False)


def test_a_horizon_selects_the_newest_run_at_or_before_the_cutoff(
    project: Path,
) -> None:
    _at(project, "2026-08-01T00:00:00+00:00")
    wanted = _at(project, "2026-08-20T00:00:00+00:00")
    _at(project, "2026-09-01T00:00:00+00:00")
    with get_db(project) as conn:
        found = store.run_at_or_before(conn, "2026-08-26T00:00:00+00:00")
    assert found is not None
    assert found.id == wanted


def test_a_run_exactly_on_the_cutoff_counts_as_at_or_before(project: Path) -> None:
    wanted = _at(project, "2026-08-26T00:00:00+00:00")
    with get_db(project) as conn:
        found = store.run_at_or_before(conn, "2026-08-26T00:00:00+00:00")
    assert found is not None and found.id == wanted


def test_a_horizon_nothing_reaches_back_to_selects_nothing(project: Path) -> None:
    """Not the oldest run as a consolation. A comparison labelled '1m'
    that quietly reached back three days would report calm it did not
    measure."""
    _at(project, "2026-09-01T00:00:00+00:00")
    with get_db(project) as conn:
        assert store.run_at_or_before(conn, "2026-08-01T00:00:00+00:00") is None


def test_earliest_run_is_what_bounds_every_horizon(project: Path) -> None:
    oldest = _at(project, "2026-08-01T00:00:00+00:00")
    _at(project, "2026-09-01T00:00:00+00:00")
    with get_db(project) as conn:
        found = store.earliest_run(conn)
        assert found is not None and found.id == oldest
        assert store.earliest_run(conn) != store.latest_run(conn)


def test_no_history_has_no_earliest_run(project: Path) -> None:
    with get_db(project) as conn:
        assert store.earliest_run(conn) is None
