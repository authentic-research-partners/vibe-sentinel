"""Versioned results: completion, captured identity and privacy projections."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from vibe_sentinel.config import SentinelConfig
from vibe_sentinel.results import (
    build_result,
    capture_provenance,
    measured_environment,
    parse_result,
    project_result,
)
from vibe_sentinel.schemas import (
    Change,
    DriftReport,
    GateFinding,
    GateReport,
    GateState,
    HorizonDrift,
    LensResult,
    Observation,
    ProbeResult,
    RunProvenance,
    ScanContext,
    Snapshot,
)
from vibe_sentinel.templates import Probe


def snapshot() -> Snapshot:
    return Snapshot(
        context=ScanContext(
            provenance=RunProvenance(
                availability="captured",
                fingerprint_status="captured",
                scanner_executable=sys.executable,
            ),
            baseline_run_id=1,
        ),
        probes={"shape": ProbeResult(probe_id="shape")},
    )


def test_complete_result_requires_completed_measurements_and_judges() -> None:
    current = snapshot()
    report = DriftReport(lenses=[LensResult(id="lens", asked=False)])
    result = build_result(current, report)
    assert result.outcome == "complete"
    assert result.judges[0].status == "not_asked"
    assert result.comparison == "compared"
    report.lenses[0].asked = True
    result = build_result(current, report)
    assert (result.outcome, result.exit_code) == ("incomplete", 2)
    assert result.failed_stages == ("judgement",)
    current.probes["shape"].ok = False
    result = build_result(current, report)
    assert result.outcome == "error"
    assert result.comparison == "incomplete"
    assert "measurement" in result.failed_stages


def test_baseline_coverage_and_first_scan_are_distinct() -> None:
    current = snapshot()
    assert current.context is not None
    current.context.baseline_unmeasured = ("shape",)
    assert build_result(current, DriftReport()).outcome == "incomplete"
    current.context.baseline_unmeasured = ()
    current.context.baseline_run_id = None
    result = build_result(current, DriftReport(first_run=True))
    assert result.outcome == "complete"
    assert result.comparison == "no_baseline"
    assert build_result(current, DriftReport()).outcome == "incomplete"


def test_failed_judge_and_unrun_gates_do_not_claim_completion() -> None:
    current = snapshot()
    assert current.context is not None
    current.context.stage_error = "judgement"
    result = build_result(current, DriftReport(), gates_ran=False)
    assert result.failed_stages == ("judgement",)
    assert result.gates_status == "not_run"
    assert result.exit_code == 2
    gates = GateState(
        reports=(
            GateReport(gate="licenses", ok=False),
            GateReport(gate="custom", configured=False),
        )
    )
    result = build_result(current, DriftReport(), gates)
    assert result.gates_status == "error"
    assert result.gates[1].status == "unconfigured"
    assert "gates" in result.failed_stages


def test_explicit_version_and_complete_envelope_required() -> None:
    payload = build_result(snapshot(), DriftReport()).model_dump(mode="json")
    assert parse_result(payload).schema_version == 3
    for invalid in (
        {},
        {**payload, "schema_version": 4},
        {**payload, "schema_version": True},
        {**payload, "exit_code": 2},
    ):
        with pytest.raises(ValueError, match="Incompatible versioned"):
            parse_result(invalid)
    del payload["schema_version"]
    with pytest.raises(ValueError):
        parse_result(payload)


def test_auth_never_participates_in_fingerprint(tmp_path: Path) -> None:
    config = SentinelConfig(project_dir=tmp_path)
    first = capture_provenance(tmp_path, config, [])
    config.api_key = uuid4().hex
    config.llm_endpoint = (
        f"http://user:{uuid4().hex}@localhost:11434/v1?access={uuid4().hex}#fragment"
    )
    second = capture_provenance(tmp_path, config, [])
    assert first.config_sha256 == second.config_sha256
    assert second.judge_endpoint == "http://localhost:11434/v1"
    assert config.api_key not in second.model_dump_json()
    assert second.source_kind == "non_git"
    assert second.source_identity_policy == "canonical_path_only"
    assert second.consistency == "not_atomic"
    config.extra_body = {"reasoning_effort": "high"}
    assert capture_provenance(tmp_path, config, []).config_sha256 != first.config_sha256


def test_policy_and_rule_content_participate_with_external_config(
    tmp_path: Path,
) -> None:
    external = tmp_path / "outside.toml"
    external.write_text("")
    config = SentinelConfig(project_dir=tmp_path, config_path=external)
    root_config = tmp_path / ".vibe-sentinel.toml"
    root_config.write_text('[probes]\nrule_files=["rules.toml"]\n')
    rule = tmp_path / "rules.toml"
    rule.write_text('[[probe]]\nid="one"\n')
    policy = tmp_path / "security" / "license-policy.toml"
    policy.parent.mkdir()
    policy.write_text('allowed_spdx=["MIT"]\n')
    first = capture_provenance(tmp_path, config, [])
    policy.write_text('allowed_spdx=["GPL-3.0-only"]\n')
    second = capture_provenance(tmp_path, config, [])
    assert second.rules_sha256 != first.rules_sha256
    rule.write_text('[[probe]]\nid="two"\n')
    assert capture_provenance(tmp_path, config, []).rules_sha256 != second.rules_sha256
    probe = Probe(id="a", title="A", command=["one"])
    assert (
        capture_provenance(tmp_path, config, [probe]).rules_sha256
        != second.rules_sha256
    )


def test_measured_environment_requires_recorded_builtin_producer() -> None:
    current = snapshot()
    dependency = ProbeResult(
        probe_id="dependency-versions",
        command=[sys.executable, "-m", "vibe_sentinel.probes.dependencies"],
        observations=[
            Observation(
                key="environment:identity",
                value=1,
                attrs={
                    "executable": "/target/python",
                    "python": "3.12.8",
                    "selection": "configured",
                },
            )
        ],
    )
    current.probes[dependency.probe_id] = dependency
    assert measured_environment(current)["python"] == "3.12.8"  # type: ignore[index]
    dependency.command = ["untrusted"]
    assert measured_environment(current) is None
    dependency.command = [sys.executable, "-m", "vibe_sentinel.probes.dependencies"]
    dependency.ok = False
    assert measured_environment(current) is None


def test_shareable_is_counts_statuses_and_hashes_only() -> None:
    canary = "arbitrary-private-value-" + uuid4().hex
    current = snapshot()
    assert current.context is not None
    current.context.provenance.judge_configured_name = canary
    current.context.provenance.scanner_executable = canary
    current.context.provenance.config_sha256 = "a" * 64
    current.probes = {
        canary: ProbeResult(
            probe_id=canary,
            summary=canary,
            error=canary,
            observations=[Observation(key=canary, value=4)],
        )
    }
    report = DriftReport(
        assessment=canary,
        changes=[Change(probe_id=canary, key=canary, kind="appeared", note=canary)],
        lenses=[LensResult(id=canary, note=canary)],
    )
    gates = GateState(reports=(GateReport(gate=canary, error=canary, summary=canary),))
    result = build_result(current, report, gates)
    shared = project_result(result, current, report, gates, profile="shareable")
    assert canary not in json.dumps(shared)
    assert set(shared) == {"result"}
    parsed = parse_result(shared["result"])
    assert parsed.provenance.config_sha256 == "a" * 64
    assert parsed.counts.observations == 1
    local = project_result(result, current, report, gates)
    assert set(local) == {"result", "snapshot", "drift", "gates"}
    assert "context" not in local["snapshot"]


def test_mcp_omits_inventory_drift_credentials_and_diagnostics() -> None:
    current = snapshot()
    dependency = Change(
        probe_id="dependency-versions", key="private-package", kind="appeared"
    )
    shape = Change(
        probe_id="shape", key="modules", kind="appeared", note="useful judgment"
    )
    report = DriftReport(
        changes=[dependency, shape],
        horizons=[HorizonDrift(horizon="1w", changes=[dependency])],
        lenses=[LensResult(id="one", note="raw diagnostic")],
    )
    finding = GateFinding(
        gate="credentials",
        key="one",
        kind="secret",
        subject="a.py",
        label="candidate",
        detail="private excerpt",
        attrs={"private": "value"},
    )
    gates = GateState(
        reports=(
            GateReport(
                gate="credentials",
                error="private diagnostic",
                summary="private diagnostic",
                findings=(finding,),
            ),
        )
    )
    projected = project_result(
        build_result(current, report, gates), current, report, gates, profile="mcp"
    )
    encoded = json.dumps(projected)
    assert "private-package" not in encoded
    assert "private excerpt" not in encoded
    assert "private diagnostic" not in encoded
    assert "raw diagnostic" not in encoded
    assert "useful judgment" in encoded
    assert "snapshot" not in projected


def test_wire_rejects_contradictory_success() -> None:
    payload = build_result(snapshot(), DriftReport()).model_dump(mode="json")
    payload["probes"][0]["status"] = "error"
    with pytest.raises(ValueError, match="Incompatible"):
        parse_result(payload)
    payload = build_result(snapshot(), DriftReport()).model_dump(mode="json")
    payload["failed_stages"] = ["measurement"]
    with pytest.raises(ValueError, match="Incompatible"):
        parse_result(payload)


def test_multiple_failed_phases_survive_and_unavailable_gate_is_error() -> None:
    current = snapshot()
    assert current.context is not None
    current.context.stage_error = "judgement"
    current.context.stage_errors = ("gates", "storage")
    result = build_result(current, DriftReport(), gates_ran=False)
    assert set(result.failed_stages) == {"judgement", "gates", "storage"}
    assert result.gates_status == "error"
    assert parse_result(result.model_dump(mode="json")).outcome == "error"


@pytest.mark.parametrize("verdict", ["typosquat", "unclear"])
def test_answered_package_finding_is_not_failed_judgement(verdict: str) -> None:
    finding = GateFinding(
        gate="packages",
        key="pair",
        kind="near-miss",
        verdict=verdict,
        failing=True,
        adjudicated=True,
    )
    gates = GateState(reports=(GateReport(gate="packages", findings=(finding,)),))
    result = build_result(snapshot(), DriftReport(), gates)
    assert result.outcome == "findings"
    assert result.exit_code == 1
    assert result.judges[0].status == "answered"
    finding.adjudicated = False
    finding.verdict = "standing"
    result = build_result(snapshot(), DriftReport(), gates)
    assert result.outcome == "incomplete"
    assert result.exit_code == 2


def test_effective_scan_windows_change_scope_fingerprint(tmp_path: Path) -> None:
    config = SentinelConfig(project_dir=tmp_path)
    default = capture_provenance(tmp_path, config, [])
    overridden = capture_provenance(tmp_path, config, [], horizons=[], trend_runs=0)
    assert overridden.scope_sha256 != default.scope_sha256
    assert overridden.config_sha256 != default.config_sha256


def test_wire_does_not_synthesize_nested_coverage_or_impossible_stage_states() -> None:
    payload = build_result(snapshot(), DriftReport()).model_dump(mode="json")
    with pytest.raises(ValueError):
        parse_result({**payload, "counts": {}})
    with pytest.raises(ValueError):
        parse_result({**payload, "provenance": {"availability": "captured"}})
    payload["probes"][0]["status"] = "answered"
    with pytest.raises(ValueError):
        parse_result(payload)


@pytest.mark.parametrize("first_run", [True, False])
@pytest.mark.parametrize("gate_name", ["packages", "credentials"])
def test_unconfigured_gate_judge_roundtrips_completed_result(
    first_run: bool, gate_name: str
) -> None:
    current = snapshot()
    assert current.context is not None
    if first_run:
        current.context.baseline_run_id = None
    # The collector deliberately reports unconfigured gates as ok=False:
    # they have not passed, but no judgement was requested or interrupted.
    gates = GateState(reports=(GateReport(gate=gate_name, configured=False, ok=False),))
    result = build_result(current, DriftReport(first_run=first_run), gates)
    assert (result.outcome, result.exit_code) == ("complete", 0)
    assert result.gates[0].status == "unconfigured"
    assert result.judges[0].status == "not_asked"
    assert parse_result(result.model_dump(mode="json")) == result
    shared = project_result(
        result, current, DriftReport(first_run=first_run), gates, profile="shareable"
    )
    assert parse_result(shared["result"]).outcome == "complete"


@pytest.mark.parametrize("gate_name", ["packages", "credentials"])
def test_configured_failed_gate_judge_is_still_not_run(gate_name: str) -> None:
    gates = GateState(reports=(GateReport(gate=gate_name, configured=True, ok=False),))
    result = build_result(snapshot(), DriftReport(), gates)
    assert (result.outcome, result.exit_code) == ("error", 2)
    assert result.gates[0].status == "error"
    assert result.judges[0].status == "not_run"
    assert parse_result(result.model_dump(mode="json")) == result
    forged = result.model_dump(mode="json")
    forged.update(outcome="complete", exit_code=0)
    with pytest.raises(ValueError, match="Incompatible versioned"):
        parse_result(forged)
