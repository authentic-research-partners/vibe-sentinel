"""Captured scan provenance and explicit local, MCP and shareable projections.

Capture is a timestamped observation, not an atomic source/config snapshot. A
configured judge name identifies routing, never immutable model weights. The
shareable profile deliberately exports counts and hashed identities, not findings.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError

from vibe_sentinel.analyze import Lens
from vibe_sentinel.config import SentinelConfig
from vibe_sentinel.packs import LoadedPack
from vibe_sentinel.rulesets import rule_file_paths
from vibe_sentinel.schemas import (
    NODE_GATE_UNAVAILABLE,
    DriftReport,
    GateState,
    ResultCounts,
    ResultPhase,
    PackProvenance,
    PackExecutableProvenance,
    ResultStage,
    RunProvenance,
    VersionedScanResult,
    decode_result,
    ScanResult,
    Snapshot,
    ScanEvidence,
    GateDecisionEvidence,
    GateEvidence,
    ChangeEvidence,
    StructuralEvidence,
    EVIDENCE_MAX_BYTES,
)
from vibe_sentinel.templates import Probe


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _endpoint(value: str) -> str:
    """Discard authentication and request-specific URL material before hashing."""
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port else ""
        return urlunsplit((parts.scheme, host + port, parts.path, "", ""))
    except ValueError:
        return ""


def _behavior(value: Any) -> Any:
    """Exclude auth values so fingerprints cannot verify guessed credentials."""
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(
                word in normalized
                for word in (
                    "api_key",
                    "password",
                    "authorization",
                    "access_token",
                    "auth_token",
                    "secret_key",
                )
            ) or normalized in {
                "headers",
                "token",
                "auth",
                "environment",
                "env",
                "env_overrides",
            }:
                continue
            # Extra request bodies can contain arbitrary provider auth fields.
            # Only declared decoder controls have a portable safe meaning.
            if normalized in {"extra_body", "llm_extra_body"} and isinstance(
                item, dict
            ):
                clean[key] = {
                    k: v
                    for k, v in item.items()
                    if k
                    in {
                        "temperature",
                        "top_p",
                        "top_k",
                        "seed",
                        "min_p",
                        "repeat_penalty",
                        "reasoning_effort",
                        "verbosity",
                    }
                    and isinstance(v, (int, float, bool, str))
                }
            elif normalized in {"endpoint", "llm_endpoint"}:
                clean[key] = _endpoint(str(item))
            else:
                clean[key] = _behavior(item)
        return clean
    if isinstance(value, (list, tuple)):
        return [_behavior(item) for item in value]
    if isinstance(value, str) and "://" in value:
        # This also covers direct dependency URLs and command URL arguments.
        return _endpoint(value)
    return value


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(root),
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def capture_provenance(
    root: Path,
    config: SentinelConfig,
    probes: list[Probe],
    lenses: tuple[Lens, ...] | None = None,
    *,
    horizons: list[str] | None = None,
    trend_runs: int | None = None,
    declared_packs: tuple[LoadedPack, ...] = (),
) -> RunProvenance:
    """Capture before measurement; failures are unavailable facts, not guesses."""
    known_packs = {pack.identity.id: pack for pack in declared_packs}
    for probe in probes:
        if not probe._pack_id:
            continue
        pack = known_packs.get(probe._pack_id)
        declaration = (
            next((raw for raw in pack.probes if raw["id"] == probe.id), None)
            if pack
            else None
        )
        if (
            pack is None
            or declaration is None
            or probe._pack_manifest_sha256 != pack.manifest_sha256
            or probe.command != declaration["command"]
        ):
            raise ValueError(
                "Pack probes require matching loaded catalog evidence; pass selected catalog.probes and declared_packs=catalog.packs from the same load"
            )
    effective = config.model_copy(
        update={
            "drift_horizons": config.drift_horizons if horizons is None else horizons,
            "drift_trend_runs": config.drift_trend_runs
            if trend_runs is None
            else trend_runs,
        }
    )
    provenance = RunProvenance(
        captured_at=datetime.now(UTC).isoformat(timespec="seconds"),
        availability="captured",
        packs=tuple(
            PackProvenance(
                id=pack.identity.id,
                version=pack.identity.version,
                protocol=pack.identity.protocol,
                manifest_sha256=pack.manifest_sha256,
                executables=tuple(
                    PackExecutableProvenance(
                        id=exe.id, command=exe.command, path=exe.path
                    )
                    for exe in pack.executables
                ),
                ecosystems=pack.identity.ecosystems,
                supported_inputs=pack.identity.supported_inputs,
                selected_probes=tuple(
                    probe.id for probe in probes if probe._pack_id == pack.identity.id
                ),
                inventory_readers=tuple(reader["id"] for reader in pack.readers),
                inventory_status="declared_not_assessed"
                if pack.readers
                else "not_declared",
            )
            for pack in declared_packs
        ),
        source_identity_sha256=_digest(str(root.resolve())),
        scanner_executable=sys.executable,
        scanner_python=platform.python_version(),
        judge_configured_name=config.llm_model,
        judge_endpoint=_endpoint(config.llm_endpoint),
        judge_identity_sha256=_digest(
            [config.llm_model, _endpoint(config.llm_endpoint)]
        ),
    )
    try:
        provenance.tool_version = importlib.metadata.version("vibe-sentinel")
    except importlib.metadata.PackageNotFoundError:
        pass
    git_root = _git(root, "rev-parse", "--show-toplevel")
    if git_root is not None:
        provenance.source_kind = "git"
        provenance.source_identity_policy = "git_head_and_path"
        revision = _git(root, "rev-parse", "--verify", "HEAD")
        provenance.source_revision = (
            revision if revision and re.fullmatch(r"[0-9a-f]{40,64}", revision) else ""
        )
        dirty = _git(root, "status", "--porcelain", "--untracked-files=normal")
        provenance.source_dirty = bool(dirty) if dirty is not None else None
    elif not any((parent / ".git").exists() for parent in (root, *root.parents)):
        provenance.source_kind = "non_git"
        provenance.source_identity_policy = "canonical_path_only"
    try:
        raw: dict[str, Any] = {}
        if config.config_path is not None:
            raw = tomllib.loads(config.config_path.read_text())
        rule_contents = []
        for path in sorted(Path(__file__).parent.glob("*.default.toml")):
            rule_contents.append(_behavior(tomllib.loads(path.read_text())))
        project_path = root / ".vibe-sentinel.toml"
        project_raw = (
            tomllib.loads(project_path.read_text()) if project_path.is_file() else {}
        )
        declarations = [raw, project_raw]
        for policy_name in (
            "license-policy.toml",
            "package-policy.toml",
            "credential-policy.toml",
        ):
            policy_path = root / "security" / policy_name
            if policy_path.is_file():
                rule_contents.append(_behavior(tomllib.loads(policy_path.read_text())))
        for declaration_file in declarations:
            for table in ("probes", "drift", "safety", "credentials"):
                declaration = declaration_file.get(table, {})
                if isinstance(declaration, dict):
                    for path in rule_file_paths(
                        root,
                        declaration.get("rule_files", []),
                        setting=table,
                        where=config.config_path or "defaults",
                    ):
                        rule_contents.append(_behavior(tomllib.loads(path.read_text())))
        rule_contents.append(_behavior(project_raw))
        provenance.config_sha256 = _digest(
            _behavior({"effective": effective.model_dump(mode="json"), "project": raw})
        )
        provenance.rules_sha256 = _digest(
            _behavior(
                {
                    "files": rule_contents,
                    "packs": [
                        pack.model_dump(mode="json") for pack in provenance.packs
                    ],
                    "probes": [p.model_dump(mode="json") for p in probes],
                    "lenses": [lens.model_dump(mode="json") for lens in lenses]
                    if lenses is not None
                    else None,
                }
            )
        )
        provenance.scope_sha256 = _digest(
            _behavior(
                {
                    "probes": [p.model_dump(mode="json") for p in probes],
                    "horizons": effective.drift_horizons,
                    "trend_runs": effective.drift_trend_runs,
                    "environment_python": effective.environment_python,
                }
            )
        )
        provenance.fingerprint_status = "captured"
    except (OSError, ValueError, TypeError):
        # Never include parser diagnostics: they can quote sensitive config.
        provenance.fingerprint_status = "unavailable"
    return provenance


def measured_environment(snapshot: Snapshot) -> dict[str, str] | None:
    """Only the actual built-in producer may claim a measured environment."""
    probe = snapshot.probes.get("dependency-versions")
    scanner = (
        snapshot.context.provenance.scanner_executable
        if snapshot.context
        else sys.executable
    )
    if (
        probe is None
        or not probe.ok
        or probe.command[:3] != [scanner, "-m", "vibe_sentinel.probes.dependencies"]
    ):
        return None
    allowed = {
        "kind",
        "name",
        "prefix",
        "python",
        "executable",
        "selection",
        "requested_executable",
        "scanner_executable",
    }
    for observation in probe.observations:
        if observation.key != "environment:identity":
            continue
        attrs = observation.attrs
        if all(
            isinstance(attrs.get(key), str) and attrs[key]
            for key in ("executable", "python", "selection")
        ):
            return {
                key: value
                for key, value in attrs.items()
                if key in allowed and isinstance(value, str)
            }
    return None


def capture_evidence(
    snapshot: Snapshot, report: DriftReport, gates: GateState
) -> ScanEvidence:
    """Validate exact producer values at one boundary, with an explicit failure.

    Internal evidence carriers use model_construct so a size limit cannot abort
    a producer and discard its measurements. Only this validated object enters
    the result/history; no identity or question is clipped to make it fit.
    """
    captured = _evidence_fields(snapshot, report, gates)
    try:
        captured = ScanEvidence.model_validate(captured.model_dump(mode="python"))
    except ValidationError:
        return ScanEvidence(availability="unavailable", failure="limit_exceeded")
    if len(captured.model_dump_json().encode()) > EVIDENCE_MAX_BYTES:
        return ScanEvidence(availability="unavailable", failure="limit_exceeded")
    return captured


def _evidence_fields(
    snapshot: Snapshot, report: DriftReport, gates: GateState
) -> ScanEvidence:
    """A field allowlist, separate from the unrestricted local diagnostic report."""
    changes = tuple(
        ChangeEvidence.model_construct(
            probe_id=change.probe_id,
            key=change.key,
            label=change.label,
            structural=StructuralEvidence.model_construct(
                None,
                **{
                    name: value
                    for name, value in change.attrs.items()
                    if name in StructuralEvidence.model_fields
                },
            )
            if any(name in StructuralEvidence.model_fields for name in change.attrs)
            else None,
            kind=change.kind,
            before=change.before,
            after=change.after,
            before_state=change.before_state,
            after_state=change.after_state,
            severity=change.severity,
            note=change.note,
            lens_id=change.rating_lens,
        )
        for change in report.changes
        if change.probe_id != "dependency-versions"
    )
    lenses = tuple(
        lens.evidence.model_copy(
            update={
                "ratings": tuple(
                    rating
                    for rating in lens.evidence.ratings
                    if rating.probe_id != "dependency-versions"
                )
            },
            deep=True,
        )
        for lens in report.lenses
        if lens.evidence is not None
    )
    findings: list[GateEvidence] = []
    for gate in gates.reports:
        findings.extend(gate.evidence)
        for finding in gate.findings:
            matches = (
                finding.attrs.get("matches", "")
                if finding.gate == "credentials"
                else ""
            )
            findings.append(
                GateEvidence.model_construct(
                    gate=finding.gate,
                    key=finding.key,
                    kind=finding.kind,
                    subject=finding.key
                    if finding.gate == "licenses" and finding.kind == "dependency"
                    else finding.subject,
                    risk=finding.risk,
                    verdict=""
                    if finding.evidence is not None
                    and finding.evidence.status == "failed"
                    else finding.verdict,
                    failing=finding.failing,
                    reason=""
                    if finding.evidence is not None
                    and finding.evidence.status == "failed"
                    else finding.reason,
                    decision=finding.evidence or GateDecisionEvidence(),
                    matches=int(matches)
                    if matches.isascii() and matches.isdigit()
                    else None,
                    spdx=finding.attrs.get("spdx", "")
                    if finding.gate == "licenses"
                    else "",
                    suspect=finding.attrs.get("suspect", "")
                    if finding.gate == "packages" and finding.kind == "near-miss"
                    else "",
                )
            )
    return ScanEvidence.model_construct(
        availability="captured"
        if snapshot.context is not None and len(lenses) == len(report.lenses)
        else "unavailable",
        changes=changes,
        lenses=lenses,
        gates=tuple(findings),
        withheld_dependency_changes=len(report.changes) - len(changes),
    )


def build_result(
    snapshot: Snapshot,
    report: DriftReport,
    gates: GateState | None = None,
    *,
    gates_ran: bool = True,
) -> ScanResult:
    """Describe completion independently of whether any findings were emitted."""
    context = snapshot.context
    gates = gates or GateState()
    evidence = capture_evidence(snapshot, report, gates)
    probes = tuple(
        ResultStage(id=key, status="complete" if probe.ok else "error")
        for key, probe in snapshot.probes.items()
    )
    gate_states = tuple(
        ResultStage(
            id=gate.gate,
            status="unconfigured"
            if not gate.configured
            else "error"
            if not gate.ok
            else "findings"
            if gate.failing
            else "complete",
        )
        for gate in gates.reports
    )
    judges = tuple(
        ResultStage(
            id=lens.id,
            status="not_asked"
            if not lens.asked
            else "answered"
            if lens.answered
            else "incomplete",
        )
        for lens in report.lenses
    )
    gate_judges = []
    for gate in gates.reports:
        if gate.gate not in {"packages", "credentials"}:
            continue
        candidates = [
            finding
            for finding in gate.findings
            if gate.gate == "credentials" or finding.kind == "near-miss"
        ]
        status: Literal["not_run", "not_asked", "answered", "incomplete"] = "not_asked"
        if not gate.configured:
            # No judgement was requested for an unconfigured gate. Its ok=False
            # records that it did not pass, not an interrupted judge attempt.
            status = "not_asked"
        elif not gate.ok:
            status = "not_run"
        elif any(
            not finding.pinned and not finding.adjudicated for finding in candidates
        ):
            status = "incomplete"
        elif any(
            finding.evidence.status == "answered"
            if finding.evidence is not None
            else finding.adjudicated
            for finding in candidates
        ):
            status = "answered"
        gate_judges.append(ResultStage(id=f"gate:{gate.gate}", status=status))
    judges += tuple(gate_judges)
    baseline_unmeasured = len(context.baseline_unmeasured) if context else 0
    failed = (
        bool(context and (context.stage_error or context.stage_errors))
        or any(p.status == "error" for p in probes)
        or bool(gates.broken)
    )
    incomplete = (
        bool(evidence.failure)
        or context is None
        or context.provenance.availability != "captured"
        or context.provenance.fingerprint_status != "captured"
        or bool(report.unmeasured)
        or bool(baseline_unmeasured)
        or any(j.status == "incomplete" for j in judges)
        or not gates_ran
    )
    findings = report.drifted or bool(gates.failing)
    outcome: Literal["complete", "findings", "incomplete", "error"] = (
        "error"
        if failed
        else "incomplete"
        if incomplete
        else "findings"
        if findings
        else "complete"
    )
    comparison: Literal["compared", "no_baseline", "incomplete", "not_run"] = (
        "no_baseline" if report.first_run else "compared"
    )
    if context is None or (context.stage_error and not snapshot.probes):
        comparison = "not_run"
    elif (
        report.unmeasured
        or baseline_unmeasured
        or any(p.status == "error" for p in probes)
    ):
        comparison = "incomplete"
    elif not report.first_run and context.baseline_run_id is None:
        comparison = "not_run"
        if outcome == "complete":
            outcome = "incomplete"
    failed_stages: list[ResultPhase] = list(context.stage_errors) if context else []
    if context and context.stage_error:
        phases: dict[
            str,
            Literal["configuration", "measurement", "judgement", "gates", "storage"],
        ] = {
            name: name
            for name in (
                "configuration",
                "measurement",
                "judgement",
                "gates",
                "storage",
            )
        }
        phase = phases.get(context.stage_error, "measurement")
        if phase not in failed_stages:
            failed_stages.append(phase)
    if any(p.status == "error" for p in probes) and "measurement" not in failed_stages:
        failed_stages.append("measurement")
    if (
        any(j.status == "incomplete" for j in judges)
        and "judgement" not in failed_stages
    ):
        failed_stages.append("judgement")
    if gates.broken and "gates" not in failed_stages:
        failed_stages.append("gates")
    measured = measured_environment(snapshot)
    return ScanResult(
        schema_version=3,
        evidence=evidence,
        run_id=context.run_id if context else None,
        baseline_run_id=context.baseline_run_id if context else None,
        generated_at=snapshot.generated_at,
        outcome=outcome,
        exit_code=2
        if outcome in {"error", "incomplete"}
        else 1
        if outcome == "findings"
        else 0,
        comparison=comparison,
        provenance=context.provenance
        if context and isinstance(context.provenance, RunProvenance)
        else RunProvenance(),
        measured_environment=measured,
        measured_environment_sha256=_digest(measured) if measured else "",
        failed_stages=tuple(failed_stages),
        probes=probes,
        gates=gate_states,
        judges=judges,
        horizons=tuple(
            ResultStage(
                id=h.horizon, status="incomplete" if h.unavailable else "complete"
            )
            for h in report.horizons
        ),
        gates_status="error"
        if gates.broken or "gates" in failed_stages
        else "complete"
        if gates_ran
        else "not_run",
        counts=ResultCounts(
            probes=len(snapshot.probes),
            observations=sum(len(p.observations) for p in snapshot.probes.values()),
            changes=len(report.changes),
            findings=sum(len(g.findings) for g in gates.reports),
            failing_findings=len(gates.failing),
            baseline_unmeasured=baseline_unmeasured,
        ),
    )


def parse_result(payload: object) -> VersionedScanResult:
    """Validate a complete versioned envelope without echoing rejected input."""
    try:
        result = decode_result(payload)
        assert isinstance(payload, dict)
        allowed_statuses = {
            "probes": {"complete", "error"},
            "gates": {"complete", "findings", "error", "unconfigured"},
            "judges": {"answered", "not_asked", "incomplete", "not_run", "error"},
            "horizons": {"complete", "incomplete"},
        }
        for key, statuses in allowed_statuses.items():
            stages = payload.get(key)
            if not isinstance(stages, list):
                raise ValueError
            for stage in stages:
                if (
                    not isinstance(stage, dict)
                    or set(stage) != {"id", "status"}
                    or not isinstance(stage["id"], str)
                    or stage["status"] not in statuses
                ):
                    raise ValueError
        if any(type(value) is not int for value in payload["counts"].values()):
            raise ValueError
        expected = (
            2
            if result.outcome in {"error", "incomplete"}
            else 1
            if result.outcome == "findings"
            else 0
        )
        if result.exit_code != expected:
            raise ValueError
        findings = (
            result.counts.failing_findings > 0
            or any(stage.status == "findings" for stage in result.gates)
            or (
                isinstance(result, ScanResult)
                and result.evidence.availability == "captured"
                and any(change.severity != "info" for change in result.evidence.changes)
            )
        )
        if findings and result.outcome == "complete":
            raise ValueError
        error = (
            any(
                stage.status == "error"
                for stage in (*result.probes, *result.gates, *result.judges)
            )
            or result.gates_status == "error"
        )
        incomplete = (
            bool(isinstance(result, ScanResult) and result.evidence.failure)
            or bool(result.failed_stages)
            or any(
                stage.status in {"incomplete", "not_run"}
                for stage in (*result.probes, *result.judges)
            )
            or result.gates_status == "not_run"
            or result.comparison in {"not_run", "incomplete"}
            or result.provenance.availability != "captured"
            or result.provenance.fingerprint_status != "captured"
            or bool(result.counts.baseline_unmeasured)
        )
        if error and result.outcome != "error":
            raise ValueError
        if incomplete and result.outcome in {"complete", "findings"}:
            raise ValueError
        if result.comparison == "compared" and result.baseline_run_id is None:
            raise ValueError
        if result.comparison == "no_baseline" and result.baseline_run_id is not None:
            raise ValueError
        return result
    except (ValueError, TypeError):
        raise ValueError("Incompatible versioned scan result") from None


def _valid_digest(value: str) -> str:
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else ""


def _safe_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).isoformat() if value else ""
    except ValueError:
        return ""


def _shareable(result: VersionedScanResult) -> VersionedScanResult:
    provenance = result.provenance
    safe = type(provenance)(
        captured_at=_safe_time(provenance.captured_at),
        availability=provenance.availability,
        source_kind=provenance.source_kind,
        source_identity_policy=provenance.source_identity_policy,
        source_identity_sha256=_valid_digest(provenance.source_identity_sha256),
        source_revision=provenance.source_revision
        if re.fullmatch(r"[0-9a-f]{40,64}", provenance.source_revision)
        else "",
        source_dirty=provenance.source_dirty,
        config_sha256=_valid_digest(provenance.config_sha256),
        rules_sha256=_valid_digest(provenance.rules_sha256),
        scope_sha256=_valid_digest(provenance.scope_sha256),
        fingerprint_status=provenance.fingerprint_status,
        tool_version=provenance.tool_version
        if re.fullmatch(r"\d+\.\d+\.\d+(?:[a-z0-9.+-]*)", provenance.tool_version)
        else "",
        judge_identity_sha256=_valid_digest(provenance.judge_identity_sha256),
        scanner_python=provenance.scanner_python
        if re.fullmatch(r"\d+\.\d+\.\d+", provenance.scanner_python)
        else "",
    )
    if isinstance(provenance, RunProvenance) and isinstance(safe, RunProvenance):
        safe.packs = tuple(
            PackProvenance(
                id=_digest(pack.id),
                version=_digest(pack.version),
                protocol=pack.protocol,
                manifest_sha256=_valid_digest(pack.manifest_sha256),
                executables=tuple(
                    PackExecutableProvenance(
                        id=_digest(exe.id),
                        command=_digest(exe.command),
                        path=_digest(exe.path),
                    )
                    for exe in pack.executables
                ),
                ecosystems=tuple(_digest(value) for value in pack.ecosystems),
                supported_inputs=tuple(
                    _digest(value) for value in pack.supported_inputs
                ),
                selected_probes=tuple(_digest(value) for value in pack.selected_probes),
                inventory_readers=tuple(
                    _digest(value) for value in pack.inventory_readers
                ),
                inventory_status=pack.inventory_status,
            )
            for pack in provenance.packs
        )
    return result.model_copy(
        update={
            "profile": "shareable",
            **(
                {
                    "evidence": ScanEvidence(
                        availability="withheld", failure=result.evidence.failure
                    )
                }
                if isinstance(result, ScanResult)
                else {}
            ),
            "generated_at": _safe_time(result.generated_at),
            "provenance": safe,
            "measured_environment": None,
            "measured_environment_sha256": _valid_digest(
                result.measured_environment_sha256
            ),
            **{
                name: tuple(
                    ResultStage(id=_digest(stage.id), status=stage.status)
                    for stage in getattr(result, name)
                )
                for name in ("probes", "gates", "judges", "horizons")
            },
        }
    )


def project_result(
    result: VersionedScanResult,
    snapshot: Snapshot | None = None,
    report: DriftReport | None = None,
    gates: GateState | None = None,
    *,
    profile: Literal["local", "local-detail", "mcp", "shareable"] = "local",
) -> dict[str, Any]:
    """One projection boundary: local inventories never enter shared exports."""
    if profile == "shareable":
        return {"result": _shareable(result).model_dump(mode="json")}
    if profile == "local-detail":
        if (
            not isinstance(result, ScanResult)
            or result.evidence.availability != "captured"
        ):
            raise ValueError(
                "Captured evidence is unavailable for this run; export the aggregate shareable result instead"
            )
        return {
            "result": result.model_copy(update={"profile": profile}).model_dump(
                mode="json"
            )
        }
    projected = result.model_copy(update={"profile": profile})
    if profile == "mcp" and isinstance(projected, ScanResult):
        projected.evidence = ScanEvidence(
            availability="withheld", failure=projected.evidence.failure
        )
    output: dict[str, Any] = {"result": projected.model_dump(mode="json")}
    if profile == "local":
        if snapshot is not None:
            output["snapshot"] = snapshot.model_dump(mode="json")
        if report is not None:
            output["drift"] = report.model_dump(mode="json")
        if gates is not None:
            output["gates"] = gates.model_dump(mode="json")
        return output
    if report is not None:
        drift = report.model_dump(mode="json")
        drift["changes"] = [
            change
            for change in drift["changes"]
            if change["probe_id"] != "dependency-versions"
        ]
        for horizon in drift["horizons"]:
            if horizon["unavailable"]:
                horizon["unavailable"] = (
                    "Comparison unavailable; diagnostics remain local."
                )
            horizon["changes"] = [
                change
                for change in horizon["changes"]
                if change["probe_id"] != "dependency-versions"
            ]
        drift["trends"] = [
            trend
            for trend in drift["trends"]
            if trend["probe_id"] != "dependency-versions"
        ]
        for lens in drift["lenses"]:
            if not lens["answered"]:
                lens["note"] = ""
        output["drift"] = drift
    if gates is not None:
        state = gates.model_dump(mode="json")
        for gate in state["reports"]:
            if gate["error"] != NODE_GATE_UNAVAILABLE:
                gate["error"] = ""
            gate["summary"] = ""
            for finding in gate["findings"]:
                if gate["gate"] == "credentials" or finding["gate"] == "credentials":
                    finding["detail"] = ""
                    finding["attrs"] = {}
        output["gates"] = state
    return output
