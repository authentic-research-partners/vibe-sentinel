"""Opt-in parameter guards use recorded argv/parameters without rewriting history."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vibe_sentinel import engine
from vibe_sentinel.cli import run_trend
from vibe_sentinel.db import get_db
from vibe_sentinel.db import store
from vibe_sentinel.schemas import DriftReport, Observation, ProbeResult, Snapshot
from vibe_sentinel.templates import Placeholder, Probe


def scoped(value: str, *, guarded: bool = True, name: str = "scoped") -> Probe:
    return Probe(
        id=name,
        title=name,
        compare_parameters=guarded,
        command=[
            "python",
            "-c",
            "import json,sys; print(json.dumps(dict(observations=[dict(key='value',value=int(sys.argv[1]))])))",
            "{VALUE}",
        ],
        placeholders=[
            Placeholder(name="VALUE", description="fixture input scope", default=value)
        ],
    )


def scan(root: Path, probes: list[Probe], **kwargs):
    return asyncio.run(engine.scan_and_compare(probes, root, lenses=(), **kwargs))


def test_scope_changes_guard_only_opted_probes_and_update_accepts_new_scope(
    tmp_path: Path,
) -> None:
    scan(tmp_path, [scoped("1"), scoped("1", guarded=False, name="legacy")])
    probes = [scoped("9"), scoped("9", guarded=False, name="legacy")]
    snapshot, report, _ = scan(tmp_path, probes)
    assert snapshot.context is not None
    assert snapshot.context.baseline_unmeasured == ("scoped",)
    assert not any(
        change.probe_id == "scoped" and change.before is not None
        for change in report.changes
    )
    assert any(
        change.probe_id == "legacy" and change.before == 1 and change.after == 9
        for change in report.changes
    )
    scan(tmp_path, probes, update=True)
    snapshot, report, _ = scan(tmp_path, probes)
    assert not snapshot.context.baseline_unmeasured
    assert not report.changes


def test_changed_command_is_a_different_opted_measurement_definition(
    tmp_path: Path,
) -> None:
    original = scoped("1")
    scan(tmp_path, [original])
    changed = original.model_copy(
        update={
            "command": [
                *original.command[:2],
                original.command[2] + "; pass",
                original.command[3],
            ]
        }
    )
    snapshot, report, _ = scan(tmp_path, [changed])
    assert snapshot.context.baseline_unmeasured == ("scoped",)
    assert all(change.severity == "info" for change in report.changes)


def test_horizons_omit_both_ends_of_changed_scope_but_keep_comparable_probes(
    tmp_path: Path,
) -> None:
    _, _, run_id = scan(
        tmp_path, [scoped("1"), scoped("1", guarded=False, name="legacy")]
    )
    with get_db(tmp_path) as conn:
        old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
        conn.execute("UPDATE runs SET started_at=? WHERE id=?", (old, run_id))
        conn.commit()
        probes = [scoped("3"), scoped("3", guarded=False, name="legacy")]
        current = engine.scan(probes, tmp_path)
        (horizon,) = engine.drift_over_horizons(conn, current, probes, ["1w"])
    assert "scoped" in horizon.unavailable
    assert not any(change.probe_id == "scoped" for change in horizon.changes)
    assert any(
        change.probe_id == "legacy" and change.kind == "grew"
        for change in horizon.changes
    )


def recorded(scope: str, value: float) -> Snapshot:
    return Snapshot(
        probes={
            "scoped": ProbeResult(
                probe_id="scoped",
                command=["python", "measure.py", scope],
                filled={"ROOT": scope},
                observations=[
                    Observation(key="value", value=value),
                    Observation(key="other", value=value + 1),
                ],
            )
        }
    )


def test_trend_filter_uses_exact_recorded_scope_and_caches_per_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with get_db(tmp_path) as conn:
        for name, value in [("a", 1), ("b", 100), ("a", 2), ("b", 200), ("a", 3)]:
            store.save_run(conn, recorded(name, value), DriftReport(), None, False)
        seen = []
        queries = []
        original = store.matching_probe_runs

        def matching(*args, **kwargs):
            queries.append(1)
            return original(*args, **kwargs)

        def fit(probe_id, key, points, **kwargs):
            seen.append([point.value for point in points])
            return None

        monkeypatch.setattr(store, "matching_probe_runs", matching)
        monkeypatch.setattr(engine.trends, "fit_series", fit)
        engine.fit_trends(
            conn,
            recorded("a", 4),
            limit_runs=50,
            min_runs=2,
            compare_parameters=frozenset({"scoped"}),
        )
        assert seen == [[2, 3, 4], [1, 2, 3]]
        assert len(queries) == 1
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 5


def test_standalone_trend_uses_current_declared_scope_without_running_probe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".vibe-sentinel.toml").write_text("""[probes]
use_builtins=false
[[probe]]
id="scoped"
title="Scoped"
compare_parameters=true
command=["python","measure.py","{ROOT}"]
[[probe.placeholders]]
name="ROOT"
description="input scope"
default="a"
""")
    (tmp_path / "measure.py").write_text(
        "raise RuntimeError('must not execute trend probe')\n"
    )
    with get_db(tmp_path) as conn:
        for name, value in [("a", 1), ("b", 100), ("a", 2)]:
            store.save_run(conn, recorded(name, value), DriftReport(), None, False)
    args = argparse.Namespace(
        root=str(tmp_path), config=None, key="value", probe="scoped", limit=30
    )
    assert run_trend(args) == 0
    output = capsys.readouterr().out
    assert "current declared" in output
    assert "run    1" in output and "run    3" in output
    assert "run    2" not in output
