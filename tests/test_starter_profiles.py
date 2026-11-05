"""Starter configurations execute the existing protocol on real JS/TS trees."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from vibe_sentinel.analyze import load_lenses, unknown_watches
from vibe_sentinel.config import load_config
from vibe_sentinel.inventory import compare
from vibe_sentinel.runner import run_probe
from vibe_sentinel.schemas import Snapshot
from vibe_sentinel.templates import Probe, load_probes


PROFILES = Path(__file__).resolve().parents[1] / "docs" / "profiles"


def _load(name: str, root: Path) -> list[Probe]:
    path = root / ".vibe-sentinel.toml"
    path.write_text((PROFILES / f"{name}.toml").read_text())
    load_config(path)
    probes = load_probes(project_root=root)
    lenses = load_lenses(root)
    assert {lens.id for lens in lenses} == {"organization"}
    assert unknown_watches(lenses, {probe.id for probe in probes}) == []
    return probes


def _write(root: Path, relative: str, content: str | bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)


def _measure(probes: list[Probe], root: Path) -> Snapshot:
    results = {probe.id: run_probe(probe, probe.defaults(), root) for probe in probes}
    assert all(result.ok for result in results.values()), {
        name: result.error for name, result in results.items() if not result.ok
    }
    return Snapshot(root=str(root), probes=results)


@pytest.mark.parametrize("profile", ["node-service", "frontend"])
def test_profiles_measure_source_changes_and_exclude_dependencies(
    tmp_path: Path, profile: str
) -> None:
    _write(tmp_path, "src/service.ts", "fetch('/items');\n")
    _write(tmp_path, "src/client.js", "fetch('/items');\n")
    _write(tmp_path, "src/App.tsx", "const app = <main />;\nfetch('/items');\n")
    _write(tmp_path, "public/logo.png", b"\x89PNG\xff")
    for excluded in ("node_modules/dep", "src/node_modules/dep", "dist", ".next"):
        _write(tmp_path, f"{excluded}/noise.ts", "fetch('/noise');\n" * 20)
        _write(tmp_path, f"{excluded}/noise.js", "fetch('/noise');\n" * 20)
        _write(tmp_path, f"{excluded}/noise.tsx", "fetch('/noise');\n" * 20)
        _write(tmp_path, f"{excluded}/noise.png", b"\xff" * 100)

    probes = _load(profile, tmp_path)
    before = _measure(probes, tmp_path)
    assert set(before.probes) == (
        {"source-lines", "fetch-typescript", "fetch-javascript"}
        | ({"fetch-tsx", "source-asset-bytes"} if profile == "frontend" else set())
    )
    lengths = before.probes["source-lines"].by_key()
    assert set(lengths) == {
        "lines:src/service.ts",
        "lines:src/client.js",
        "lines:src/App.tsx",
        "not-measured",
    }
    assert lengths["lines:src/service.ts"].value == 1
    assert lengths["lines:src/App.tsx"].value == 2
    for probe_id in ("fetch-typescript", "fetch-javascript"):
        assert before.probes[probe_id].by_key()["pattern-total"].value == 1
    if profile == "frontend":
        assert before.probes["fetch-tsx"].by_key()["pattern-total"].value == 1
        assets = before.probes["source-asset-bytes"].by_key()
        assert set(assets) == {"bytes:public/logo.png", "not-measured"}
        assert assets["bytes:public/logo.png"].value == 5

    _write(tmp_path, "src/service.ts", "fetch('/items');\nfetch('/more');\n")
    _write(
        tmp_path,
        "src/App.tsx",
        "const app = <main />;\nfetch('/items');\nfetch('/more');\n",
    )
    _write(tmp_path, "public/logo.png", b"\x89PNG\xff" * 2)
    after = _measure(probes, tmp_path)
    report = compare(before, after, probes)
    changed = {(change.probe_id, change.key) for change in report.changes}
    assert ("source-lines", "lines:src/service.ts") in changed
    assert ("source-lines", "lines:src/App.tsx") in changed
    assert ("fetch-typescript", "pattern-total") in changed
    if profile == "frontend":
        assert ("source-asset-bytes", "bytes:public/logo.png") in changed
        assert ("fetch-tsx", "pattern-total") in changed


@pytest.mark.parametrize("profile", ["node-service", "frontend"])
def test_empty_source_is_a_failed_measurement(tmp_path: Path, profile: str) -> None:
    probe = next(p for p in _load(profile, tmp_path) if p.id == "source-lines")
    result = run_probe(probe, probe.defaults(), tmp_path)
    assert not result.ok
    assert "matched any category" in result.error


def test_missing_asset_root_fails_instead_of_reporting_zero(tmp_path: Path) -> None:
    probe = next(p for p in _load("frontend", tmp_path) if p.id == "source-asset-bytes")
    result = run_probe(probe, {"ASSET_ROOT": "missing"}, tmp_path)
    assert not result.ok
    assert "not a directory" in result.error


def test_missing_ast_grep_fails_instead_of_reporting_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "src/service.ts", "fetch('/items');\n")
    probe = next(
        p for p in _load("node-service", tmp_path) if p.id == "fetch-typescript"
    )
    # Isolate the missing external tool from Python executable discovery.
    probe = probe.model_copy(update={"command": [sys.executable, *probe.command[1:]]})
    monkeypatch.setenv("PATH", "")
    result = run_probe(probe, probe.defaults(), tmp_path)
    assert not result.ok
    assert "ast-grep not found" in result.error
