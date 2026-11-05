"""The plugin protocol preserves CLI outcomes, process boundaries and opt-ins."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from vibe_sentinel import mcp_server
from vibe_sentinel.db import get_db
from vibe_sentinel.db.store import save_run
from vibe_sentinel.inventory import compare
from vibe_sentinel.results import build_result, project_result
from vibe_sentinel.schemas import (
    NODE_GATE_UNAVAILABLE,
    Change,
    DriftReport,
    GateFinding,
    GateReport,
    GateState,
    Observation,
    ProbeResult,
    RunProvenance,
    ScanContext,
    Snapshot,
)


def _context() -> ScanContext:
    return ScanContext(
        provenance=RunProvenance(
            availability="captured", fingerprint_status="captured"
        ),
        run_id=2,
        baseline_run_id=1,
    )


def _versioned(
    snapshot: Snapshot, drift: DriftReport, gates: GateState
) -> dict[str, Any]:
    return project_result(build_result(snapshot, drift, gates), snapshot, drift, gates)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".vibe-sentinel.toml").write_text('[safety]\nmode = "off"\n')
    return tmp_path


@pytest.mark.parametrize("root", [".", "relative/project"])
def test_root_cannot_inherit_server_config(root: str) -> None:
    with pytest.raises(ValueError, match="absolute"):
        asyncio.run(mcp_server.history(root))


def test_unconfigured_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires"):
        asyncio.run(mcp_server.history(str(tmp_path)))


@pytest.mark.parametrize(
    ("exit_code", "output", "outcome"),
    [
        (0, '{"drift": {"changes": []}, "gates": {"reports": []}}', "completed"),
        (1, '{"drift": {"changes": []}, "gates": {"reports": []}}', "findings"),
        (2, '{"drift": {"changes": []}, "gates": {"reports": []}}', "error"),
        (2, "", "error"),
        (0, "not JSON", "error"),
        (0, "", "error"),
        (0, '{"gates": {}}', "error"),
        (
            0,
            '{"drift": {"changes": "private-input"}, "gates": {"reports": []}}',
            "error",
        ),
        (0, "null", "error"),
    ],
)
def test_scan_preserves_cli_result(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    output: str,
    outcome: str,
) -> None:
    if output == '{"drift": {"changes": []}, "gates": {"reports": []}}':
        snapshot = Snapshot(context=_context(), probes={"p": ProbeResult(probe_id="p")})
        drift = DriftReport()
        if exit_code == 1:
            drift.changes = [
                Change(
                    probe_id="p",
                    key="a",
                    kind="grew",
                    before=1,
                    after=2,
                    severity="low",
                )
            ]
        elif exit_code == 2:
            snapshot.probes["failed"] = ProbeResult(probe_id="failed", ok=False)
        output = json.dumps(_versioned(snapshot, drift, GateState()))
    launch = asyncio.create_subprocess_exec
    observed: list[tuple[str, ...]] = []

    async def substitute(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        observed.append(args)
        assert kwargs["cwd"] == project
        return await launch(
            sys.executable,
            "-c",
            "import sys; sys.stdout.write(sys.argv[1]); "
            "sys.stderr.write('diagnostic'); sys.exit(int(sys.argv[2]))",
            output,
            str(exit_code),
            **kwargs,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", substitute)
    result = asyncio.run(mcp_server.scan(str(project), since="1w,1m", fit=0))
    assert result.exit_code == exit_code
    assert result.outcome == outcome
    assert result.stdout == ""
    assert "diagnostics that remain local" in result.stderr
    assert result.interpreter == sys.executable
    if result.data is not None:
        assert bool(result.data["drift"]["changes"]) is (exit_code == 1)
        assert result.data["gates"]["reports"] == []
    assert "private-input" not in result.model_dump_json()
    assert observed[0][:4] == (sys.executable, "-m", "vibe_sentinel", "scan")
    assert "--config" in observed[0]
    assert "--since=1w,1m" in observed[0]
    assert "--fit=0" in observed[0]
    assert "--update" not in observed[0]


def test_scan_explains_missing_baseline_coverage(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context()
    context.baseline_unmeasured = ("pattern-census",)
    snapshot = Snapshot(
        context=context,
        probes={"pattern-census": ProbeResult(probe_id="pattern-census")},
    )
    gates = GateState(
        reports=[
            GateReport(
                gate="licenses",
                findings=[GateFinding(gate="licenses", key="example", failing=True)],
            )
        ]
    )
    payload = _versioned(snapshot, DriftReport(), gates)

    async def response(*args: Any, **kwargs: Any) -> mcp_server.CommandResult:
        return mcp_server.CommandResult(
            root=str(project),
            interpreter=sys.executable,
            exit_code=2,
            outcome="error",
            data=payload,
            stderr="private-diagnostic",
        )

    monkeypatch.setattr(mcp_server, "_run", response)
    result = asyncio.run(mcp_server.scan(str(project)))
    assert result.outcome == "incomplete"
    assert result.exit_code == 2
    assert result.summary == (
        "Scan incomplete. Baseline run 1 lacks comparable data for 1 probe."
    )
    assert result.data["result"]["failed_stages"] == []
    assert result.data["result"]["probes"][0]["status"] == "complete"
    assert result.data["result"]["counts"]["failing_findings"] == 1
    assert "private-diagnostic" not in result.model_dump_json()


def test_scan_accepts_retired_probes_and_preserves_gate_status(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = Snapshot(
        context=_context(), probes={"frontend": ProbeResult(probe_id="frontend")}
    )
    baseline = Snapshot(probes={"python": ProbeResult(probe_id="python")})
    gates = GateState(
        reports=(
            GateReport(
                gate="licenses",
                ok=False,
                configured=False,
                error=NODE_GATE_UNAVAILABLE,
            ),
            GateReport(
                gate="credentials",
                findings=(
                    GateFinding(
                        gate="credentials",
                        key="example",
                        failing=True,
                        adjudicated=True,
                    ),
                ),
            ),
        )
    )
    payload = _versioned(snapshot, compare(baseline, snapshot, []), gates)

    async def response(*args: Any, **kwargs: Any) -> mcp_server.CommandResult:
        return mcp_server.CommandResult(
            root=str(project),
            interpreter=sys.executable,
            exit_code=1,
            outcome="findings",
            data=payload,
        )

    monkeypatch.setattr(mcp_server, "_run", response)
    result = asyncio.run(mcp_server.scan(str(project)))
    assert result.outcome == "findings"
    assert result.data["result"]["counts"]["failing_findings"] == 1
    assert result.data["result"]["evidence"]["availability"] == "withheld"
    assert result.data["gates"]["reports"][0]["error"] == NODE_GATE_UNAVAILABLE
    assert {change["kind"] for change in result.data["drift"]["changes"]} == {
        "appeared",
        "disappeared",
    }


def test_subprocess_start_failure_is_explicit(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        raise OSError("test launch failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)
    result = asyncio.run(mcp_server.history(str(project)))
    assert result.outcome == "error"
    assert result.exit_code is None
    assert "test launch failure" in result.stderr


@pytest.mark.parametrize("exit_code", [0, 1, 2])
def test_scan_exports_findings_without_inventory_or_credential_excerpt(
    project: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    snapshot = Snapshot(
        context=_context(),
        probes={
            "dependency-versions": ProbeResult(
                probe_id="dependency-versions",
                observations=[
                    Observation(
                        key="version:private-installed-package-sentinel", state="9.8.7"
                    )
                ],
            )
        },
    )
    drift = DriftReport(
        changes=[Change(probe_id="size", key="module", kind="grew", before=1, after=2)]
        if exit_code
        else []
    )
    gates = GateState(
        reports=(
            GateReport(
                gate="credentials",
                findings=(
                    GateFinding(
                        gate="credentials",
                        key="secret:file",
                        subject="settings.py",
                        detail="private-credential-excerpt-sentinel",
                        verdict="secret",
                        adjudicated=True,
                        failing=exit_code != 0,
                        reason="A credential is stored in source",
                        attrs={"line": "12"},
                    ),
                ),
                summary="One credential requires a decision",
            ),
            GateReport(
                gate="packages",
                ok=exit_code != 2,
                error="Policy could not be read" if exit_code == 2 else "",
            ),
        )
    )
    snapshot.probes["size"] = ProbeResult(probe_id="size")
    report = _versioned(snapshot, drift, gates)

    async def response(*args: Any, **kwargs: Any) -> mcp_server.CommandResult:
        return mcp_server.CommandResult(
            root=str(project),
            interpreter=sys.executable,
            exit_code=exit_code,
            outcome="completed"
            if exit_code == 0
            else "findings"
            if exit_code == 1
            else "error",
            stdout=json.dumps(report),
            data=report,
            stderr="private-probe-diagnostic-sentinel",
        )

    monkeypatch.setattr(mcp_server, "_run", response)
    result = asyncio.run(mcp_server.scan(str(project)))
    encoded = result.model_dump_json()
    assert "private-installed-package-sentinel" not in encoded
    assert "private-credential-excerpt-sentinel" not in encoded
    assert "private-probe-diagnostic-sentinel" not in encoded
    assert result.stdout == ""
    assert result.exit_code == exit_code
    assert "Inspect the local CLI output or project logs" in result.stderr
    if exit_code:
        assert result.data["drift"]["changes"][0]["key"] == "module"
    else:
        assert result.data["drift"]["changes"] == []
    credential, broken = result.data["gates"]["reports"]
    assert credential["findings"][0]["subject"] == "settings.py"
    assert credential["findings"][0]["failing"] is (exit_code != 0)
    assert credential["findings"][0]["attrs"] == {}
    assert not credential["findings"][0].get("detail")
    assert broken["ok"] is (exit_code != 2)
    assert not broken["error"] or "local" in broken["error"]


@pytest.mark.parametrize("exit_code", [2, -9])
def test_scan_does_not_export_partial_stdout_on_failure(
    project: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    async def response(*args: Any, **kwargs: Any) -> mcp_server.CommandResult:
        return mcp_server.CommandResult(
            root=str(project),
            interpreter=sys.executable,
            exit_code=exit_code,
            outcome="error",
            stdout='{"snapshot": "private-partial-report-sentinel',
            stderr="private-timeout-diagnostic-sentinel",
        )

    monkeypatch.setattr(mcp_server, "_run", response)
    result = asyncio.run(mcp_server.scan(str(project)))
    assert "private-partial-report-sentinel" not in result.model_dump_json()
    assert "private-timeout-diagnostic-sentinel" not in result.model_dump_json()
    assert "Inspect the local CLI output or project logs" in result.stderr
    assert result.data is None
    assert result.stdout == ""
    assert result.exit_code == exit_code
    assert result.outcome == "error"


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancellation_reap_cli(
    project: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    launch = asyncio.create_subprocess_exec
    children: list[asyncio.subprocess.Process] = []

    async def exercise() -> None:
        started = asyncio.Event()

        async def substitute(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            process = await launch(
                sys.executable, "-c", "import time; time.sleep(60)", **kwargs
            )
            children.append(process)
            started.set()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", substitute)
        monkeypatch.setattr(mcp_server, "COMMAND_TIMEOUT", 60.0 if cancel else 0.02)
        request = asyncio.create_task(mcp_server.history(str(project)))
        await started.wait()
        if cancel:
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        else:
            result = await request
            assert result.outcome == "error"
            assert "timed out" in result.stderr
        assert children[0].returncode is not None

    asyncio.run(exercise())


def test_stdio_round_trip_history_and_mode(project: Path) -> None:
    """A real MCP client talks to the installed server and CLI, without a model."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    (project / ".vibe-sentinel.toml").write_text('[safety]\nmode = "observe"\n')
    with get_db(project) as conn:
        run_id = save_run(
            conn,
            Snapshot(root=str(project), model="recorded-test-model"),
            DriftReport(root=str(project)),
            baseline_run_id=None,
            make_baseline=False,
        )

    async def exercise() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "vibe_sentinel.mcp_server"],
            cwd=str(project),
            env=dict(os.environ),
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                listing = await client.list_tools()
                tools = {tool.name: tool for tool in listing.tools}
                assert set(tools) == {
                    "scan",
                    "security",
                    "security_setup",
                    "security_status",
                    "history",
                    "trends",
                    "commands",
                    "safety_history",
                    "status",
                    "configure_safety",
                }
                assert tools["scan"].annotations is not None
                assert tools["scan"].annotations.readOnlyHint is False
                assert tools["scan"].annotations.destructiveHint is True
                assert tools["history"].annotations is not None
                assert tools["history"].annotations.readOnlyHint is False
                assert tools["security"].annotations is not None
                assert tools["security"].annotations.readOnlyHint is True
                assert tools["security_status"].annotations is not None
                assert tools["security_status"].annotations.readOnlyHint is True
                assert tools["security_setup"].annotations is not None
                assert tools["security_setup"].annotations.openWorldHint is True
                missing_tools = project / "missing-tools"
                tool_status = await client.call_tool(
                    "security_status",
                    {"root": str(project), "tools_directory": str(missing_tools)},
                )
                assert not tool_status.isError
                assert tool_status.structuredContent is not None
                assert tool_status.structuredContent["outcome"] == "incomplete"
                assert tool_status.structuredContent["exit_code"] == 2
                assert not missing_tools.exists()
                security_report = await client.call_tool(
                    "security",
                    {"root": str(project), "browser_artifacts": "missing-export"},
                )
                assert not security_report.isError
                assert security_report.structuredContent is not None
                assert security_report.structuredContent["outcome"] == "incomplete"
                assert security_report.structuredContent["exit_code"] == 2
                assert security_report.structuredContent["stdout"] == ""
                assert security_report.structuredContent["stderr"] == ""
                assert security_report.structuredContent["data"]["checks"][2][
                    "status"
                ] == ("unavailable")
                response = await client.call_tool(
                    "history", {"root": str(project), "run": run_id}
                )
                assert not response.isError
                assert response.structuredContent is not None
                assert response.structuredContent["outcome"] == "completed"
                assert "recorded-test-model" in response.structuredContent["stdout"]
                assert response.structuredContent["interpreter"] == sys.executable
                missing = await client.call_tool(
                    "history", {"root": str(project), "run": run_id + 1}
                )
                assert missing.structuredContent is not None
                assert missing.structuredContent["outcome"] == "error"
                assert missing.structuredContent["exit_code"] == 2
                configured = await client.call_tool(
                    "configure_safety", {"root": str(project), "mode": "journal"}
                )
                assert not configured.isError
                assert configured.structuredContent is not None
                assert configured.structuredContent["outcome"] == "completed"
                refused = await client.call_tool(
                    "configure_safety", {"root": str(project), "mode": "off"}
                )
                assert refused.isError

    asyncio.run(exercise())
    import tomllib

    config = tomllib.loads((project / ".vibe-sentinel.toml").read_text())
    assert config["safety"]["mode"] == "off"  # journal is the CLI's off mode
    assert not (project / ".claude").exists()
    assert not (project / ".cursor").exists()


def test_missing_optional_sdk_has_install_remediation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(mcp_server.importlib.util, "find_spec", lambda name: None)
    assert mcp_server.main() == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert 'uv pip install "vibe-sentinel[plugin]"' in output.err


def test_concurrent_requests_keep_their_project_config_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = [tmp_path / "first", tmp_path / "second"]
    for project in projects:
        project.mkdir()
        (project / ".vibe-sentinel.toml").write_text('[safety]\nmode = "off"\n')
        with get_db(project) as conn:
            save_run(
                conn,
                Snapshot(root=str(project), model=project.name),
                DriftReport(root=str(project)),
                baseline_run_id=None,
                make_baseline=False,
            )

    async def exercise() -> list[mcp_server.CommandResult]:
        return list(
            await asyncio.gather(*(mcp_server.history(str(p), run=1) for p in projects))
        )

    monkeypatch.chdir(tmp_path)

    def forbid_chdir(path: Any) -> None:
        raise AssertionError("A request must not change the server's working directory")

    monkeypatch.setattr(os, "chdir", forbid_chdir)
    results = asyncio.run(exercise())
    for project, result in zip(projects, results, strict=True):
        assert result.outcome == "completed"
        assert result.root == str(project)
        assert f"model: {project.name}" in result.stdout


def test_cancellation_during_launch_does_not_orphan_process(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch = asyncio.create_subprocess_exec

    async def exercise() -> None:
        creating = asyncio.Event()
        finish_creation = asyncio.Event()
        children: list[asyncio.subprocess.Process] = []

        async def substitute(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            creating.set()
            await finish_creation.wait()
            child = await launch(
                sys.executable, "-c", "import time; time.sleep(60)", **kwargs
            )
            children.append(child)
            return child

        monkeypatch.setattr(asyncio, "create_subprocess_exec", substitute)
        request = asyncio.create_task(mcp_server.history(str(project)))
        await creating.wait()
        request.cancel()
        finish_creation.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert len(children) == 1
        assert children[0].returncode is not None

    asyncio.run(exercise())


@pytest.mark.parametrize("corruption", ["version", "missing", "status", "scanner"])
def test_incompatible_contract_is_withheld(corruption: str) -> None:
    snapshot = Snapshot(context=_context())
    payload = _versioned(snapshot, DriftReport(), GateState())
    if corruption == "version":
        payload["result"]["schema_version"] = 99
    elif corruption == "missing":
        del payload["snapshot"]["probes"]
    elif corruption == "status":
        payload["result"]["exit_code"] = 1
    else:
        payload["result"]["provenance"]["scanner_executable"] = "/private/scanner"
    payload["private-extra"] = "untrusted-private-payload"
    response = mcp_server._scan_projection(
        mcp_server.CommandResult(
            root="/project",
            interpreter=sys.executable,
            exit_code=0,
            outcome="completed",
            stdout=json.dumps(payload),
            data=payload,
        )
    )
    assert response.outcome == "error"
    assert response.data is None
    assert response.stdout == ""
    assert "untrusted-private-payload" not in response.model_dump_json()
