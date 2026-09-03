"""The scan: fill, run, compare, analyze, record.

Nothing clever lives in ``engine.py``, which is the point — so what these
pin is the orchestration's two promises. Every scan is recorded, because
the history is the product; and ``--update`` controls only which run
later scans compare *against*, so a scan that finds drift never quietly
becomes the baseline it just failed.

No model is involved anywhere here. ``use_model=False`` is the CI path
and it has to work with nothing running, which also makes the whole
pipeline testable without a GPU.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from vibe_sentinel.db import get_db
from vibe_sentinel.db import store as db_store
from vibe_sentinel.engine import drift_over_horizons, scan, scan_and_compare
from vibe_sentinel.schemas import DriftReport, Observation, ProbeResult, Snapshot
from vibe_sentinel.templates import Placeholder, Probe


def _probe(probe_id: str = "p", key: str = "a", value: float = 1.0) -> Probe:
    """A probe that prints one observation and exits 0."""
    payload = json.dumps(
        {"observations": [{"key": key, "value": value, "label": key}], "summary": key}
    )
    return Probe(
        id=probe_id, title="t", command=["python", "-c", f"print({payload!r})"]
    )


def _broken(probe_id: str = "bad") -> Probe:
    return Probe(
        id=probe_id, title="t", command=["python", "-c", "raise SystemExit(3)"]
    )


def _run(probes: list[Probe], root: Path, **kw: Any):
    return asyncio.run(scan_and_compare(probes, root, use_model=False, **kw))


# --- the scan itself -------------------------------------------------------


def test_a_scan_collects_each_probe_s_observations(tmp_path: Path) -> None:
    snapshot = scan([_probe()], tmp_path, use_model=False)
    result = snapshot.probes["p"]
    assert result.ok is True
    assert [o.key for o in result.observations] == ["a"]
    assert result.title == "t"
    assert snapshot.used_model is False


def test_measuring_never_reaches_the_model(tmp_path: Path) -> None:
    """This is the CI path, and it is now the only path.

    Nothing decides what to measure but the config: `scan` is synchronous
    because there is nothing in it to await. The model rates what changed
    and judges a credential candidate; it never chooses the target.
    """
    import inspect

    assert not inspect.iscoroutinefunction(scan)
    snapshot = scan([_probe()], tmp_path, use_model=False)
    assert snapshot.probes["p"].ok is True
    # No model, so no model name to record — and nothing may read as one.
    assert snapshot.model == ""


def test_a_probe_runs_with_the_values_its_config_declares(tmp_path: Path) -> None:
    probe = Probe(
        id="p",
        title="t",
        command=["python", "-c", 'print(\'{"observations": [], "summary": "{X}"}\')'],
        placeholders=[Placeholder(name="X", description="a word", default="fallback")],
    )
    snapshot = scan([probe], tmp_path, use_model=False)
    assert snapshot.probes["p"].filled == {"X": "fallback"}


def test_a_failed_probe_is_recorded_not_raised(tmp_path: Path) -> None:
    """One broken template must not lose the other probes' measurements,
    and a silently missing probe reads as 'nothing changed' next time."""
    snapshot = scan([_probe(), _broken()], tmp_path, use_model=False)
    assert snapshot.probes["p"].ok is True
    assert snapshot.probes["bad"].ok is False
    assert "exit 3" in snapshot.probes["bad"].error


# --- recording -------------------------------------------------------------


def test_every_scan_is_recorded(tmp_path: Path) -> None:
    _, _, first = _run([_probe()], tmp_path)
    _, _, second = _run([_probe(value=2.0)], tmp_path)
    with get_db(tmp_path) as conn:
        assert [r.id for r in db_store.list_runs(conn)] == [second, first]


def test_the_first_run_becomes_the_baseline(tmp_path: Path) -> None:
    """It has nothing to compare against, so there is nothing to accept."""
    _, report, run_id = _run([_probe()], tmp_path)
    assert report.first_run is True
    with get_db(tmp_path) as conn:
        baseline = db_store.baseline_run(conn)
    assert baseline is not None and baseline.id == run_id


def test_drift_does_not_move_the_baseline(tmp_path: Path) -> None:
    """The failure this prevents: a scan that finds drift becoming the
    thing the next scan compares against, so the drift is never seen
    again. Accepting a structural change is a deliberate act."""
    _, _, first = _run([_probe(value=1.0)], tmp_path)
    _, report, second = _run([_probe(value=9.0)], tmp_path)

    assert report.drifted is True
    with get_db(tmp_path) as conn:
        baseline = db_store.baseline_run(conn)
    assert baseline is not None and baseline.id == first != second


def test_update_moves_the_baseline(tmp_path: Path) -> None:
    _run([_probe(value=1.0)], tmp_path)
    _, _, second = _run([_probe(value=9.0)], tmp_path, update=True)
    with get_db(tmp_path) as conn:
        baseline = db_store.baseline_run(conn)
    assert baseline is not None and baseline.id == second


def test_a_recorded_run_can_be_read_back_whole(tmp_path: Path) -> None:
    _, _, run_id = _run([_probe(key="dir:src", value=3.0)], tmp_path)
    with get_db(tmp_path) as conn:
        snapshot = db_store.load_snapshot(conn, run_id)
    assert snapshot is not None
    observation = snapshot.probes["p"].observations[0]
    assert (observation.key, observation.value) == ("dir:src", 3.0)


def test_drift_is_measured_against_the_baseline_not_the_previous_run(
    tmp_path: Path,
) -> None:
    """Three runs, no --update: the third is still compared against the
    first, so a change does not stop being reported by happening twice."""
    _run([_probe(value=1.0)], tmp_path)
    _run([_probe(value=9.0)], tmp_path)
    _, report, _ = _run([_probe(value=9.0)], tmp_path)
    assert [c.kind for c in report.changes] == ["grew"]
    assert (report.changes[0].before, report.changes[0].after) == (1.0, 9.0)


def test_a_run_without_a_model_is_recorded_as_unanalyzed(tmp_path: Path) -> None:
    """``runs.analyzed`` is what stops a later reader claiming these
    severities were reviewed."""
    _, report, run_id = _run([_probe(value=1.0)], tmp_path)
    _, report, run_id = _run([_probe(value=9.0)], tmp_path)
    assert report.analyzed is False
    with get_db(tmp_path) as conn:
        (record,) = [r for r in db_store.list_runs(conn) if r.id == run_id]
    assert record.analyzed is False
    assert record.used_model is False


# --- horizons: drift too slow for any single comparison ---------------------


def _tolerant(value: float, tolerance: float) -> Probe:
    """A probe that reports ``value`` and ignores movement under ``tolerance``."""
    probe = _probe(value=value)
    return Probe(
        id=probe.id, title=probe.title, command=probe.command, tolerance=tolerance
    )


def _recorded(root: Path, days_ago: float, value: float) -> int:
    """Put one run in the history, stamped ``days_ago``."""
    at = datetime.now(UTC) - timedelta(days=days_ago)
    snapshot = Snapshot(
        generated_at=at.isoformat(timespec="seconds"),
        root=str(root),
        probes={
            "p": ProbeResult(
                probe_id="p",
                observations=[Observation(key="a", value=value, label="a")],
            )
        },
    )
    with get_db(root) as conn:
        run_id = db_store.save_run(
            conn, snapshot, DriftReport(), None, make_baseline=False
        )
    return run_id


def _baseline_is_latest(root: Path) -> None:
    with get_db(root) as conn:
        latest = db_store.latest_run(conn)
        assert latest is not None
        db_store.mark_baseline(conn, latest.id)


def test_a_horizon_catches_what_no_single_comparison_can(tmp_path: Path) -> None:
    """The whole point. Every step is inside the tolerance and the total
    is not, so the baseline comparison is calm on every scan while the
    month is a reorganization."""
    for days, value in ((40, 4.0), (20, 6.0), (1, 9.0)):
        _recorded(tmp_path, days, value)
    _baseline_is_latest(tmp_path)

    _, report, _ = _run(
        [_tolerant(10.0, tolerance=2.0)], tmp_path, horizons=["1w", "1m"]
    )

    assert report.changes == []  # nothing moved since yesterday's baseline
    assert report.drifted is False
    horizons = {w.horizon: w for w in report.horizons}
    assert [c.before for c in horizons["1w"].changes] == [6.0]
    assert [c.before for c in horizons["1m"].changes] == [4.0]
    assert horizons["1m"].moved is True
    # The horizon names the run it actually reached, which is older than
    # the horizon itself wherever the history is sparse.
    assert horizons["1w"].age_days > 7


def test_a_horizon_never_reaches_the_exit_code(tmp_path: Path) -> None:
    """A month-old finding would otherwise fail every scan for a month
    with nothing that clears it — `--update` moves the baseline, and the
    horizon still reaches back to the same fixed point."""
    _recorded(tmp_path, 40, 1.0)
    _recorded(tmp_path, 0, 99.0)
    _baseline_is_latest(tmp_path)

    _, report, _ = _run([_probe(value=99.0)], tmp_path, horizons=["1m"])
    assert report.horizons[0].moved is True
    assert report.drifted is False


def test_a_horizon_is_not_stored(tmp_path: Path) -> None:
    """Both ends already are, so this diff recomputes exactly — unlike
    the measurements, which only re-run against the code as it is now.
    The database keeps what cannot be regenerated."""
    _recorded(tmp_path, 40, 1.0)
    _recorded(tmp_path, 0, 5.0)
    _baseline_is_latest(tmp_path)

    _, report, run_id = _run([_probe(value=9.0)], tmp_path, horizons=["1m"])
    assert [c.before for c in report.horizons[0].changes] == [1.0]

    with get_db(tmp_path) as conn:
        stored = db_store.load_changes(conn, run_id)
        counted = conn.execute(
            "SELECT change_count FROM runs WHERE id = ?", (run_id,)
        ).fetchone()[0]
    assert [(c.key, c.before) for c in stored] == [("a", 5.0)]
    assert counted == len(report.changes)


def test_a_horizon_nothing_reaches_says_so_rather_than_reading_as_clean(
    tmp_path: Path,
) -> None:
    _run([_probe(value=1.0)], tmp_path)  # the baseline
    _, report, _ = _run([_probe(value=1.0)], tmp_path, horizons=["1y"])
    horizon = report.horizons[0]
    assert horizon.run_id is None
    assert horizon.changes == []
    assert horizon.moved is False
    assert "no run recorded 1y or more ago" in horizon.unavailable
    assert "history starts at" in horizon.unavailable


def test_the_first_run_has_no_horizon_to_look_through(tmp_path: Path) -> None:
    """There is nothing behind a baseline that does not exist yet, and
    every horizon would report the same emptiness a second time."""
    _, report, _ = _run([_probe()], tmp_path, horizons=["1w", "1m"])
    assert report.first_run is True
    assert report.horizons == []


def test_no_horizons_declared_is_the_previous_behaviour_exactly(tmp_path: Path) -> None:
    _run([_probe(value=1.0)], tmp_path)
    _, report, _ = _run([_probe(value=2.0)], tmp_path, horizons=[])
    assert report.horizons == []
    assert report.drifted is True


def test_two_horizons_landing_on_one_run_read_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sparse history — a repository scanned monthly answers `1w` and
    `2w` with the same run, whose several hundred observations should not
    be loaded twice."""
    _recorded(tmp_path, 40, 1.0)
    loads: list[int] = []
    real = db_store.load_snapshot

    def counted(conn: Any, run_id: int) -> Any:
        loads.append(run_id)
        return real(conn, run_id)

    monkeypatch.setattr(db_store, "load_snapshot", counted)
    current = scan([_probe(value=2.0)], tmp_path, use_model=False)
    with get_db(tmp_path) as conn:
        horizons = drift_over_horizons(conn, current, [_probe()], ["1w", "2w"])

    assert [w.run_id for w in horizons] == [horizons[0].run_id] * 2
    assert loads == [horizons[0].run_id]


# --- trends: the direction no comparison of two points can see -------------


def _history(root: Path, values: list[float], key: str = "a") -> None:
    """Record one run per value, oldest first."""
    for value in values:
        _recorded_value(root, key, value)


def _recorded_value(root: Path, key: str, value: float) -> int:
    snapshot = Snapshot(
        root=str(root),
        probes={
            "p": ProbeResult(
                probe_id="p",
                observations=[Observation(key=key, value=value, label=key)],
            )
        },
    )
    with get_db(root) as conn:
        return db_store.save_run(
            conn, snapshot, DriftReport(), None, make_baseline=False
        )


def test_a_scan_reports_the_direction_while_every_step_is_small(
    tmp_path: Path,
) -> None:
    """The reason this exists. No step clears the tolerance, so the
    baseline comparison is calm on every scan, and the series has
    doubled."""
    _history(tmp_path, [4.0 + 0.4 * i for i in range(20)])
    _baseline_is_latest(tmp_path)

    _, report, _ = _run([_tolerant(11.6, tolerance=1.0)], tmp_path, trend_runs=50)
    assert report.changes == []
    assert report.drifted is False
    assert len(report.trends) == 1
    fit = report.trends[0]
    assert fit.direction == "rising"
    assert fit.significant is True
    assert fit.slope == pytest.approx(0.4, abs=0.05)


def test_a_trend_never_reaches_the_exit_code(tmp_path: Path) -> None:
    """Same reasoning as a horizon. A slope persists for as many scans as
    the window is long, and nothing anyone could do would clear it."""
    _history(tmp_path, [4.0 + 0.4 * i for i in range(20)])
    _baseline_is_latest(tmp_path)
    _, report, _ = _run([_tolerant(11.6, tolerance=1.0)], tmp_path, trend_runs=50)
    assert report.trends[0].significant is True
    assert report.drifted is False


def test_a_trend_is_not_stored(tmp_path: Path) -> None:
    _history(tmp_path, [4.0 + 0.4 * i for i in range(20)])
    _baseline_is_latest(tmp_path)
    _, report, run_id = _run([_tolerant(11.6, tolerance=1.0)], tmp_path, trend_runs=50)
    assert report.trends
    with get_db(tmp_path) as conn:
        assert db_store.load_changes(conn, run_id) == []


def test_the_scan_scores_what_it_just_measured_out_of_sample(
    tmp_path: Path,
) -> None:
    """The value being judged is not in the fit that judges it. A point
    included in its own fit pulls the line towards itself and then
    reports how near it is to where it pulled it."""
    _history(tmp_path, [4.0 + 0.5 * i for i in range(25)])
    _baseline_is_latest(tmp_path)

    _, report, _ = _run([_tolerant(400.0, tolerance=1000.0)], tmp_path, trend_runs=50)
    assert report.changes == []  # the tolerance swallows it whole
    assert len(report.trends) == 1
    assert [a.value for a in report.trends[0].anomalies] == [400.0]
    # Not yet recorded, so it has no run of its own.
    assert report.trends[0].anomalies[0].run_id is None


def test_the_scan_does_not_repeat_an_anomaly_from_inside_the_window(
    tmp_path: Path,
) -> None:
    """A jump at run 12 stays in a fifty-run window for fifty scans, and
    something reported on every scan with nothing that settles it is the
    shape this codebase has already been wrong about once. `trend` lists
    them; a scan reports what changed for this scan."""
    values = [4.0 + 0.5 * i for i in range(25)]
    values[12] = 90.0
    _history(tmp_path, values)
    _baseline_is_latest(tmp_path)

    _, report, _ = _run([_tolerant(16.5, tolerance=1000.0)], tmp_path, trend_runs=50)
    assert all(a.run_id is None for f in report.trends for a in f.anomalies)


def test_a_steady_number_is_not_reported_as_a_finding(tmp_path: Path) -> None:
    """There are hundreds of those, and a fit of a number doing nothing
    is not news."""
    _history(tmp_path, [7.0, 7.0, 8.0, 7.0, 7.0, 8.0, 7.0, 7.0, 8.0, 7.0, 7.0, 8.0])
    _baseline_is_latest(tmp_path)
    _, report, _ = _run([_tolerant(7.0, tolerance=5.0)], tmp_path, trend_runs=50)
    assert report.trends == []


def test_trend_runs_zero_turns_the_fits_off(tmp_path: Path) -> None:
    _history(tmp_path, [4.0 + 0.4 * i for i in range(20)])
    _baseline_is_latest(tmp_path)
    _, report, _ = _run([_tolerant(11.6, tolerance=1.0)], tmp_path, trend_runs=0)
    assert report.trends == []


def test_the_first_run_has_no_history_to_fit(tmp_path: Path) -> None:
    _, report, _ = _run([_probe()], tmp_path, trend_runs=50)
    assert report.first_run is True
    assert report.trends == []


def test_a_key_the_scan_no_longer_measures_is_not_fitted(tmp_path: Path) -> None:
    """Its disappearance is a change, which `compare` already reports.
    Fitting a series that has ended answers a question nobody asked."""
    _history(tmp_path, [4.0 + 0.4 * i for i in range(20)], key="gone")
    _baseline_is_latest(tmp_path)
    _, report, _ = _run([_probe(key="other", value=1.0)], tmp_path, trend_runs=50)
    assert report.trends == []
