"""Offline summaries preserve recorded outcomes without exporting private text."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from vibe_sentinel import cli, evidence_summary
from vibe_sentinel.__main__ import main
from vibe_sentinel.results import build_result, parse_result, project_result
from vibe_sentinel.schemas import (
    Change,
    DriftReport,
    GateFinding,
    GateReport,
    GateState,
    HorizonDrift,
    ProbeResult,
    RunProvenance,
    ScanContext,
    Snapshot,
)


def captured(outcome: str = "complete"):
    snapshot = Snapshot(
        generated_at="2026-11-03T12:00:00+00:00",
        context=ScanContext(
            run_id=7,
            baseline_run_id=4,
            provenance=RunProvenance(
                availability="captured",
                fingerprint_status="captured",
                source_kind="git",
                source_revision="a" * 40,
                source_dirty=True,
            ),
        ),
        probes={"shape": ProbeResult(probe_id="shape")},
    )
    report = DriftReport()
    if outcome == "findings":
        report.changes = [
            Change(
                probe_id="shape", key="private-path", kind="appeared", severity="high"
            )
        ]
    elif outcome == "error":
        snapshot.probes["shape"].ok = False
    elif outcome == "incomplete":
        snapshot.context.baseline_unmeasured = ("shape",)
    return build_result(snapshot, report)


@pytest.mark.parametrize(
    "outcome,code", [("complete", 0), ("findings", 1), ("incomplete", 2), ("error", 2)]
)
@pytest.mark.parametrize("wrapped", [True, False])
def test_cli_returns_recorded_exit_and_captured_identity(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
    code: int,
    wrapped: bool,
) -> None:
    result = captured(outcome)
    payload = result.model_dump(mode="json")
    path = tmp_path / "result.json"
    path.write_text(json.dumps({"result": payload} if wrapped else payload))
    assert main(["summarize", str(path)]) == code
    output = capsys.readouterr()
    assert f"Outcome: **{outcome}**" in output.out
    assert "a" * 40 in output.out
    assert "dirty at capture: **yes**" in output.out
    assert "Run: 7; baseline run: 4" in output.out
    assert "private-path" not in output.out
    assert output.err == ""


def test_no_current_config_network_scan_or_history_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Offline summary reached current project state")

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".vibe-sentinel.toml").write_text("invalid config")
    path = tmp_path / "result.json"
    path.write_text(captured().model_dump_json())
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    monkeypatch.setattr(cli, "auto_check", forbidden)
    monkeypatch.setattr(cli, "load_config_or_fail", forbidden)
    monkeypatch.setattr(cli, "get_db", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    assert main(["summarize", str(path)]) == 0
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert "Outcome" in capsys.readouterr().out


def test_local_details_and_malicious_strings_never_reach_markdown(
    tmp_path: Path,
) -> None:
    canary = "PRIVATE<script>alert(1)</script>|\n::error::injected\r\x1b[31m"
    result = captured("findings")
    result.evidence.changes[0].label = canary
    result.evidence.changes[0].note = canary
    result.provenance.source_revision = canary
    result.provenance.scanner_executable = canary
    result.provenance.judge_configured_name = canary
    result.provenance.judge_endpoint = canary
    result.generated_at = canary
    result.measured_environment = {"executable": canary}
    path = tmp_path / "local.json"
    path.write_text(json.dumps(project_result(result, profile="local-detail")))
    output = evidence_summary.render_summary(evidence_summary.read_result(path))
    for forbidden in ("PRIVATE", "<script>", "::error::", "\r", "\x1b", "private-path"):
        assert forbidden not in output
    assert "Captured revision: `unavailable`" in output
    assert "Recorded at: `unavailable`" in output


@pytest.mark.parametrize("version", [1, 2])
def test_frozen_legacy_envelopes_are_summarized_without_upgrading(version: int) -> None:
    path = Path(__file__).parent / f"fixtures/result-v{version}.json"
    result = evidence_summary.read_result(path)
    assert result.schema_version == version
    assert "Outcome:" in evidence_summary.render_summary(result)


def test_missing_baseline_horizon_and_unconfigured_gate_stay_explicit() -> None:
    snapshot = Snapshot(
        context=ScanContext(
            provenance=RunProvenance(
                availability="captured", fingerprint_status="captured"
            )
        )
    )
    report = DriftReport(
        first_run=True,
        horizons=[HorizonDrift(horizon="1w", unavailable="private diagnostic")],
    )
    gates = GateState(
        reports=(GateReport(gate="credentials", configured=False, ok=False),)
    )
    result = build_result(snapshot, report, gates)
    text = evidence_summary.render_summary(result)
    assert "No baseline comparison was available" in text
    assert "unconfigured: 1" in text and "not_asked: 1" in text
    assert "Longer horizons | incomplete: 1" in text
    assert "private diagnostic" not in text


@pytest.mark.parametrize(
    "raw",
    [
        b'{"result":{},"result":{}}',
        b'{"value":NaN}',
        b'{"value":1e999}',
        b"\xff",
        b"[]",
        b"{}",
        b'{"schema_version":999}',
        b'{"result":{},"snapshot":{"private":"raw"}}',
    ],
)
def test_invalid_input_fails_without_echoing_data(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "invalid.json"
    path.write_bytes(raw)
    output = subprocess.run(
        [sys.executable, "-m", "vibe_sentinel", "summarize", str(path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert output.returncode == 2
    assert not output.stdout
    assert "Cannot summarize this input" in output.stderr
    assert "private" not in output.stderr and "raw" not in output.stderr


@pytest.mark.parametrize("limit", ["MAX_BYTES", "MAX_NODES", "MAX_DEPTH"])
def test_resource_limits_fail_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    path = tmp_path / "result.json"
    path.write_text(captured().model_dump_json())
    monkeypatch.setattr(evidence_summary, limit, 1)
    with pytest.raises(ValueError, match="Cannot summarize"):
        evidence_summary.read_result(path)


def test_fifo_and_absent_file_fail_without_waiting(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    for path in (fifo, tmp_path / "absent", tmp_path):
        with pytest.raises(ValueError, match="Cannot summarize"):
            evidence_summary.read_result(path)


def test_contradictory_outcome_is_not_rendered(tmp_path: Path) -> None:
    payload = captured("error").model_dump(mode="json")
    payload["outcome"] = "complete"
    payload["exit_code"] = 0
    path = tmp_path / "result.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="Cannot summarize"):
        evidence_summary.read_result(path)


def test_capture_failure_is_visible_in_aggregate() -> None:
    result = captured("incomplete")
    payload = result.model_dump(mode="json")
    payload["evidence"].update(availability="unavailable", failure="limit_exceeded")
    output = evidence_summary.render_summary(parse_result(payload))
    assert "capture exceeded its bounds" in output


@pytest.mark.parametrize("known", ["failing_count", "gate_status", "captured_drift"])
def test_known_findings_cannot_be_relabeled_as_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], known: str
) -> None:
    if known == "captured_drift":
        payload = project_result(captured("findings"), profile="local-detail")["result"]
    else:
        snapshot = Snapshot(
            context=ScanContext(
                provenance=RunProvenance(
                    availability="captured", fingerprint_status="captured"
                )
            )
        )
        gate = GateReport(
            gate="licenses",
            findings=(
                GateFinding(gate="licenses", key="private-package", failing=True),
            ),
        )
        result = build_result(
            snapshot, DriftReport(first_run=True), GateState(reports=(gate,))
        )
        payload = project_result(result, profile="shareable")["result"]
        if known == "failing_count":
            payload["gates"][0]["status"] = "complete"
        else:
            payload["counts"]["failing_findings"] = 0
    payload.update(outcome="complete", exit_code=0)
    path = tmp_path / "edited.json"
    path.write_text(json.dumps({"result": payload}))
    with pytest.raises(ValueError, match="Incompatible"):
        parse_result(payload)
    assert main(["summarize", str(path)]) == 2
    assert capsys.readouterr().out == ""


def test_recorded_info_changes_do_not_imply_findings() -> None:
    result = captured("findings")
    result.evidence.changes[0].severity = "info"
    result.outcome = "complete"
    result.exit_code = 0
    assert parse_result(result.model_dump(mode="json")) == result
    assert "Outcome: **complete**" in evidence_summary.render_summary(result)


@pytest.mark.parametrize(
    "field",
    ["schema_version", "exit_code", "run_id", "baseline_run_id", "observations"],
)
@pytest.mark.parametrize("wrong", [False, 0.0, 1.5, "0"])
def test_raw_numeric_fields_are_not_coerced(
    tmp_path: Path, field: str, wrong: object
) -> None:
    payload = captured().model_dump(mode="json")
    if field == "observations":
        payload["counts"][field] = wrong
    else:
        payload[field] = wrong
    path = tmp_path / "coerced.json"
    path.write_text(json.dumps({"result": payload}))
    with pytest.raises(ValueError, match="Cannot summarize"):
        evidence_summary.read_result(path)
