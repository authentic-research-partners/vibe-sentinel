"""Every dependency consumer reads the selected environment, not the scanner."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import pytest

from vibe_sentinel import _python_inventory as reader
from vibe_sentinel import cli, engine, mcp_server
from vibe_sentinel.config import SentinelConfig, load_config
from vibe_sentinel.gates import collect_licenses
from vibe_sentinel.inventory import compare
from vibe_sentinel.licenses import explain
from vibe_sentinel.python_environment import collect_environment
from vibe_sentinel.templates import Probe, default_probes


@pytest.fixture
def target_project(tmp_path: Path) -> tuple[Path, Path]:
    """A controlled collector protocol, independent of host startup hooks.

    Actual interpreter isolation and metadata discovery belong to the collector
    tests. These consumers run real CLI/probe subprocesses against known evidence.
    """
    if os.name != "posix":
        pytest.skip("Executable collector protocol fixture requires POSIX")
    target = tmp_path / ".venv"
    python = target / "bin/python"
    python.parent.mkdir(parents=True)
    protocol = target / "collector-fixture.json"
    protocol.write_text(
        json.dumps(
            {
                "environment": {
                    "kind": "venv",
                    "name": ".venv",
                    "prefix": str(target),
                    "python": sys.version.split()[0],
                    "executable": str(python),
                },
                "distributions": [
                    {
                        "name": "target-only",
                        "version": "7.4.2",
                        "installer": "fixture",
                        "provenance_recorded": True,
                        "license_files": [
                            [
                                "LICENSE",
                                (
                                    Path(__file__).resolve().parents[1] / "LICENSE"
                                ).read_text(),
                            ]
                        ],
                    }
                ],
                "marker_environment": reader.marker_environment(),
                "stdlib_modules": sorted(sys.stdlib_module_names),
                "import_owners": {},
                "importable": [],
                "limitations": [],
            }
        )
    )
    python.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        "import sys\n"
        f"sys.stdout.write(Path({str(protocol)!r}).read_text())\n"
    )
    python.chmod(0o700)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="app"\nversion="1.0"\ndependencies=["target-only"]\n'
    )
    config = tmp_path / ".vibe-sentinel.toml"
    config.write_text(
        "[environment]\npython="
        + json.dumps(str(python))
        + '\n[licenses]\nallowed_categories=["permissive"]\n'
    )
    return tmp_path, python


def _dependency_probe() -> Probe:
    return next(
        probe for probe in default_probes() if probe.id == "dependency-versions"
    )


def test_licenses_and_explanation_use_captured_target_evidence(
    target_project: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    root, python = target_project
    snapshot = collect_environment(root)
    captured = snapshot.license_distributions()
    assert len(captured) == 1
    # Reading captured evidence must not reopen any target files.
    (root / ".venv/collector-fixture.json").unlink()
    resolved, evidence = explain(captured[0])
    assert resolved.name == "target-only"
    assert resolved.spdx == "MIT"
    assert any(item.used for item in evidence)
    assert cli._find_distribution("target_only", captured) is captured[0]
    assert cli._find_distribution("pydantic", captured) is None


def test_license_gate_and_cli_report_selected_interpreter(
    target_project: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    root, python = target_project
    gate = collect_licenses(root, config=load_config(root / ".vibe-sentinel.toml"))
    assert gate.ok
    assert "1 package(s) accepted" in gate.summary
    assert str(python) in gate.summary
    args = argparse.Namespace(root=str(root), verbose=True)
    assert cli.run_licenses(args) == 0
    output = capsys.readouterr().out
    assert "target-only" in output
    assert str(python) in output
    assert f"scanner: {sys.executable}" in output


def test_dependency_probe_honors_external_config_and_records_target_changes(
    target_project: tuple[Path, Path], tmp_path_factory: pytest.TempPathFactory
) -> None:
    root, python = target_project
    external = tmp_path_factory.mktemp("config") / "sentinel-{CONFIG}.toml"
    external.write_text("[environment]\npython=" + json.dumps(str(python)))
    (root / ".vibe-sentinel.toml").write_text(
        '[environment]\npython="missing-python"\n'
        '[licenses]\nallowed_categories=["permissive"]\n'
    )
    probe = _dependency_probe()
    config = load_config(external)
    assert collect_licenses(root, config=config).ok
    before = engine.scan([probe], root, config)
    result = before.probes[probe.id]
    assert result.ok, result.error
    observations = result.by_key()
    assert observations["version:target-only"].state == "7.4.2"
    assert "version:pydantic" not in observations
    identity = observations["environment:identity"]
    assert identity.attrs["executable"] == str(python)
    assert identity.attrs["selection"] == "configured"
    assert f"--config={external}" in result.command

    # Programmatic settings also override the conflicting root config.
    after = engine.scan([probe], root, SentinelConfig())
    assert after.probes[probe.id].ok, after.probes[probe.id].error
    report = compare(before, after, [probe])
    assert any(change.key == "environment:identity" for change in report.changes)


def test_declared_probe_scope_uses_project_selection(
    target_project: tuple[Path, Path],
) -> None:
    root, _python = target_project
    (root / "pyproject.toml").write_text(
        '[project]\nname="app"\nversion="1.0"\ndependencies=[]\n'
        '[project.optional-dependencies]\nfeature=["target-only"]\n'
    )
    config_path = root / ".vibe-sentinel.toml"
    with config_path.open("a") as stream:
        stream.write('\n[packages]\nextras=["feature"]\n')
    probe = _dependency_probe()
    for placeholder in probe.placeholders:
        if placeholder.name == "SCOPE":
            placeholder.default = "declared"
    selected = engine.scan([probe], root, load_config(config_path)).probes[probe.id]
    assert selected.ok, selected.error
    assert selected.by_key()["version:target-only"].state == "7.4.2"
    config_path.write_text(
        config_path.read_text().replace('extras=["feature"]', "extras=[]")
    )
    inactive = engine.scan([probe], root, load_config(config_path)).probes[probe.id]
    assert not inactive.ok
    assert "no dependency" in inactive.error


def test_explicit_empty_environment_is_measured_but_missing_declared_set_fails(
    target_project: tuple[Path, Path],
) -> None:
    root, _python = target_project
    protocol = root / ".venv/collector-fixture.json"
    payload = json.loads(protocol.read_text())
    payload["distributions"] = []
    protocol.write_text(json.dumps(payload))
    config = load_config(root / ".vibe-sentinel.toml")
    probe = _dependency_probe()
    all_result = engine.scan([probe], root, config).probes[probe.id]
    assert all_result.ok, all_result.error
    assert all_result.by_key()["count:installed"].value == 0
    assert (
        all_result.by_key()["environment:identity"].attrs["selection"] == "configured"
    )
    for placeholder in probe.placeholders:
        if placeholder.name == "SCOPE":
            placeholder.default = "declared"
    declared = engine.scan([probe], root, config).probes[probe.id]
    assert not declared.ok
    assert "missing installed metadata for target-only" in declared.error


def test_custom_override_command_is_not_rewritten(tmp_path: Path) -> None:
    probe = Probe(
        id="dependency-versions",
        title="Custom reader",
        source="custom.toml",
        command=[sys.executable, "-c", "print('{\"observations\": []}')"],
    )
    config = SentinelConfig(environment_python="missing-python")
    result = engine.scan([probe], tmp_path, config).probes[probe.id]
    assert result.ok
    assert result.command == probe.command


@pytest.mark.skipif(os.name != "posix", reason="Executable target wrapper")
@pytest.mark.parametrize("explicit_config", [True, False])
def test_builtin_timeout_allows_inner_collector_to_stop_its_target(
    tmp_path: Path,
    explicit_config: bool,
) -> None:
    started = tmp_path / "started"
    survived = tmp_path / "survived"
    wrapper = tmp_path / "slow-target"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import time\nfrom pathlib import Path\n"
        f"Path({str(started)!r}).touch()\n"
        "time.sleep(1.6)\n"
        f"Path({str(survived)!r}).touch()\n"
    )
    wrapper.chmod(0o700)
    config_path = tmp_path / ".vibe-sentinel.toml"
    config_path.write_text(
        "[environment]\ntimeout=1.0\npython=" + json.dumps(str(wrapper))
    )
    probe = _dependency_probe()
    probe.timeout_s = 0.8
    config = load_config(config_path) if explicit_config else None
    result = engine.scan([probe], tmp_path, config).probes[probe.id]
    assert not result.ok
    assert "inspection timed out" in result.error
    assert started.exists()
    time.sleep(0.8)
    assert not survived.exists(), "target survived its failed collector"
    assert probe.timeout_s == 0.8  # The declaration itself is not rewritten.


def test_custom_dependency_override_keeps_its_declared_timeout(tmp_path: Path) -> None:
    probe = Probe(
        id="dependency-versions",
        title="Custom reader",
        source="custom.toml",
        command=[sys.executable, "-c", "import time; time.sleep(1)"],
        timeout_s=0.05,
    )
    result = engine.scan(
        [probe], tmp_path, SentinelConfig(environment_timeout=120)
    ).probes[probe.id]
    assert not result.ok
    assert "timed out after 0.05" in result.error


def test_invalid_target_fails_license_gate_and_dependency_probe(tmp_path: Path) -> None:
    (tmp_path / ".vibe-sentinel.toml").write_text(
        '[licenses]\nallowed_categories=["permissive"]\n'
    )
    config = SentinelConfig(environment_python="missing-python")
    assert not collect_licenses(tmp_path, config=config).ok
    probe = _dependency_probe()
    result = engine.scan([probe], tmp_path, config).probes[probe.id]
    assert not result.ok
    assert "environment inspection failed" in result.error


def test_skipped_dynamic_paths_cannot_report_clean_or_removed_packages(
    target_project: tuple[Path, Path],
) -> None:
    root, _python = target_project
    config = load_config(root / ".vibe-sentinel.toml")
    probe = _dependency_probe()
    before = engine.scan([probe], root, config)
    assert before.probes[probe.id].ok
    protocol = root / ".venv/collector-fixture.json"
    payload = json.loads(protocol.read_text())
    payload["limitations"] = ["Synthetic skipped startup hook can add metadata paths"]
    protocol.write_text(json.dumps(payload))
    gate = collect_licenses(root, config=config)
    assert not gate.ok
    assert gate.configured
    assert "incomplete" in gate.error
    after = engine.scan([probe], root, config)
    assert not after.probes[probe.id].ok
    assert "coverage is incomplete" in after.probes[probe.id].error
    report = compare(before, after, [probe])
    assert report.unmeasured == [probe.id]
    assert not report.changes


def test_mcp_projects_only_successful_recorded_environment_identity() -> None:
    identity = {
        "executable": "/target/python",
        "python": "3.11.9",
        "selection": "configured",
        "scanner_executable": sys.executable,
        "private-extra": "must stay local",
    }
    payload = {
        "drift": {"changes": []},
        "gates": {"reports": []},
        "snapshot": {
            "probes": {
                "dependency-versions": {
                    "ok": True,
                    "command": [
                        sys.executable,
                        "-m",
                        "vibe_sentinel.probes.dependencies",
                    ],
                    "observations": [
                        {"key": "environment:identity", "attrs": identity},
                        {"key": "version:private-dependency", "state": "1.2"},
                    ],
                }
            }
        },
    }
    from vibe_sentinel.results import build_result, project_result
    from vibe_sentinel.schemas import (
        DriftReport,
        GateState,
        RunProvenance,
        ScanContext,
        Snapshot,
    )

    payload["snapshot"]["probes"]["dependency-versions"]["probe_id"] = (
        "dependency-versions"
    )
    snapshot = Snapshot.model_validate(payload["snapshot"])
    snapshot.context = ScanContext(
        provenance=RunProvenance(
            availability="captured",
            fingerprint_status="captured",
            scanner_executable=sys.executable,
        )
    )
    drift, gates = DriftReport(first_run=True), GateState()
    payload = project_result(
        build_result(snapshot, drift, gates), snapshot, drift, gates
    )
    result = mcp_server.CommandResult(
        root="/project",
        interpreter=sys.executable,
        exit_code=0,
        outcome="completed",
        data=payload,
    )
    projected = mcp_server._scan_projection(result)
    assert projected.interpreter == sys.executable
    assert projected.measured_environment == {
        key: value for key, value in identity.items() if key != "private-extra"
    }
    assert "private-dependency" not in projected.model_dump_json()
    assert "must stay local" not in projected.model_dump_json()

    producer = payload["snapshot"]["probes"]["dependency-versions"]
    producer["command"] = [sys.executable, "custom_probe.py"]
    result.data = payload
    assert mcp_server._scan_projection(result).measured_environment is None
    producer["command"] = [sys.executable, "-m", "vibe_sentinel.probes.dependencies"]
    payload["snapshot"]["probes"]["dependency-versions"]["ok"] = False
    result.data = payload
    assert mcp_server._scan_projection(result).measured_environment is None
