"""Normal CLI and collection entry points discover managed security resources."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import sys
from typing import Any

import pytest

from vibe_sentinel import security_checks, security_osv, security_setup, security_source
from vibe_sentinel.__main__ import main
from vibe_sentinel.config import SentinelConfig


def test_status_does_not_create_an_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "tools"
    assert (
        main(
            [
                "security",
                "status",
                "--tools-directory",
                str(directory),
                "--format",
                "json",
            ]
        )
        == 2
    )
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "unavailable"
    assert not directory.exists()
    assert list(tmp_path.iterdir()) == []


def test_failed_refresh_is_an_error_even_with_prior_ready_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failed(directory: Path | None, *, refresh: bool) -> dict[str, Any]:
        assert directory == tmp_path
        assert refresh
        return {"status": "error", "ready": True, "components": {}}

    monkeypatch.setattr(security_setup, "setup", failed)
    assert (
        main(
            [
                "security",
                "setup",
                "--refresh",
                "--tools-directory",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["ready"] is True


@pytest.mark.parametrize("prefix", [[], ["scan"]])
def test_scan_keeps_legacy_syntax(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    prefix: list[str],
) -> None:
    async def collect(
        root: Path, config: SentinelConfig, **kwargs: Any
    ) -> security_checks.SecurityReport:
        assert root == tmp_path
        assert kwargs["source"] == "src"
        assert kwargs["tools_directory"] == tmp_path / "tools"
        return security_checks.SecurityReport(
            schema_version=1,
            checks=(
                security_checks.SecurityCheck(
                    id="js-source", status="complete", summary="fixture", findings=[]
                ),
            ),
        )

    monkeypatch.setattr(security_checks, "collect", collect)
    assert (
        main(
            [
                "security",
                *prefix,
                str(tmp_path),
                "--source",
                "src",
                "--tools-directory",
                str(tmp_path / "tools"),
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["checks"][0]["status"] == "complete"


def test_collection_uses_managed_paths_without_path_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = {
        name: tmp_path / name
        for name in ("osv", "semgrep", "advisory_db", "typescript", "node")
    }
    monkeypatch.setattr(security_setup, "resolve", lambda directory: tools)
    calls = []

    def advisory(
        root: Path, lockfile: str, database: Path, **kwargs: Any
    ) -> dict[str, Any]:
        calls.append("advisories")
        assert root == tmp_path and lockfile == "package-lock.json"
        assert database == tools["advisory_db"]
        assert kwargs["executable"] == tools["osv"]
        return {
            "id": "npm-advisories",
            "status": "complete",
            "summary": "fixture",
            "findings": [],
        }

    def source(root: Path, selected: str, **kwargs: Any) -> dict[str, Any]:
        calls.append("source")
        assert root == tmp_path and selected == "src"
        assert kwargs == {
            "executable": tools["semgrep"],
            "typescript": tools["typescript"],
            "node": tools["node"],
        }
        return {
            "id": "js-source",
            "status": "complete",
            "summary": "fixture",
            "findings": [],
        }

    monkeypatch.setattr(security_osv, "check", advisory)
    monkeypatch.setattr(security_source, "check", source)
    report = asyncio.run(
        security_checks.collect(
            tmp_path,
            SentinelConfig(),
            lockfile="package-lock.json",
            advisory_db=None,
            source="src",
            browser_artifacts=None,
        )
    )
    assert report.exit_code == 0
    assert calls == ["advisories", "source"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cleanup")
def test_mcp_cancellation_stops_detached_installer(tmp_path: Path) -> None:
    from vibe_sentinel.mcp_server import _stop

    child_pid = tmp_path / "installer.pid"
    installer = (
        "import os,time; from pathlib import Path; "
        f"Path({str(child_pid)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    launcher = """
import sys
from pathlib import Path
from vibe_sentinel import security_setup
from vibe_sentinel.__main__ import main
def setup(directory, *, refresh):
    security_setup._install_command(
        [sys.executable, "-c", sys.argv[2]], Path(sys.argv[1]), Path(sys.argv[1])
    )
security_setup.setup = setup
raise SystemExit(main(["security", "setup", "--tools-directory", sys.argv[1]]))
"""

    async def exercise() -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            launcher,
            str(tmp_path),
            installer,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        communication = asyncio.create_task(process.communicate())
        try:
            for _ in range(150):
                if child_pid.is_file():
                    break
                if process.returncode is not None:
                    pytest.fail("The synthetic installer did not start")
                await asyncio.sleep(0.02)
            assert child_pid.is_file()
            pid = int(child_pid.read_text())
            await _stop(process)
            await asyncio.wait_for(communication, timeout=5)
            assert process.returncode == 130
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            if child_pid.is_file():
                try:
                    os.kill(int(child_pid.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await communication

    asyncio.run(exercise())
