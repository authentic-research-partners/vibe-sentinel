"""Explicit offline npm advisory check through an installed OSV-Scanner v2.

The database argument is the prepared cache root containing
``osv-scalibr/npm/all.zip`` for the supported OSV-Scanner v2.7+ releases.
No database download, dependency installation, or project discovery occurs.
The JSON contract and flags follow google/osv-scanner's pkg/models/results.go
and cmd/osv-scanner/internal/helper/flags.go. Raw diagnostics are discarded.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any
from urllib.parse import parse_qs, urlsplit

_OUTPUT_LIMIT = 16 * 1024 * 1024
_TIMEOUT = 120
_TOKEN = re.compile(r"[A-Za-z0-9@._/+\-]{1,214}\Z")
_ADVISORY = re.compile(r"[A-Z][A-Z0-9]{1,23}-[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_NUMBER = r"(?:0|[1-9][0-9]*)"
_PRERELEASE = rf"(?:{_NUMBER}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_VERSION = re.compile(
    rf"v?{_NUMBER}\.{_NUMBER}\.{_NUMBER}"
    rf"(?:-{_PRERELEASE}(?:\.{_PRERELEASE})*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)


def _run(argv: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    """Bound run time and parsing memory; never return stderr."""
    with tempfile.TemporaryFile() as output:
        completed = subprocess.run(  # noqa: S603 # fixed argv, no shell
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.DEVNULL,
            timeout=_TIMEOUT,
            check=False,
        )
        if output.tell() > _OUTPUT_LIMIT:
            raise ValueError("output limit")
        output.seek(0)
        return completed.returncode, output.read().decode("utf-8")


def _parse(
    payload: str, target: Path, root: Path
) -> tuple[list[dict[str, Any]], int, int]:
    """Require all-packages coverage and retain only constrained identifiers."""
    data = json.loads(payload)
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("missing results")
    findings: dict[str, dict[str, Any]] = {}
    count = 0
    unassessed = 0
    for source in data["results"]:
        if not isinstance(source, dict) or not isinstance(source.get("source"), dict):
            raise ValueError("invalid source")
        info = source["source"]
        path = info.get("path")
        if (
            not isinstance(path, str)
            or info.get("type") != "lockfile"
            or (root / path).resolve() != target
            or not isinstance(source.get("packages"), list)
        ):
            raise ValueError("unexpected source")
        for entry in source["packages"]:
            if not isinstance(entry, dict) or not isinstance(
                entry.get("package"), dict
            ):
                raise ValueError("invalid package")
            package = entry["package"]
            name, version = package.get("name"), package.get("version")
            if (
                package.get("ecosystem") != "npm"
                or not isinstance(name, str)
                or not _TOKEN.fullmatch(name)
                or not isinstance(version, str)
                or not _TOKEN.fullmatch(version)
            ):
                raise ValueError("invalid package identifier")
            count += 1
            purl = package.get("purl")
            if (
                not _VERSION.fullmatch(version)
                or not isinstance(purl, str)
                or not purl.startswith("pkg:npm/")
                or parse_qs(urlsplit(purl).query).get("source") != ["PUBLIC_REGISTRY"]
            ):
                unassessed += 1
                continue
            vulnerabilities = entry.get("vulnerabilities", [])
            if not isinstance(vulnerabilities, list):
                raise ValueError("invalid vulnerabilities")
            for vulnerability in vulnerabilities:
                advisory = (
                    vulnerability.get("id") if isinstance(vulnerability, dict) else None
                )
                if not isinstance(advisory, str) or not _ADVISORY.fullmatch(advisory):
                    raise ValueError("invalid advisory identifier")
                relative = target.relative_to(root).as_posix()
                key = hashlib.sha256(
                    json.dumps([relative, name, version, advisory]).encode()
                ).hexdigest()[:24]
                findings[key] = {
                    "id": f"npm-advisory:{key}",
                    "message": "Locked npm package matches a local OSV advisory.",
                    "path": relative,
                    "package": name,
                    "version": version,
                    "advisory": advisory,
                }
    return sorted(findings.values(), key=lambda item: item["id"]), count, unassessed


def check(
    root: Path, lockfile: str, database: Path, *, executable: Path | None = None
) -> dict[str, Any]:
    """Check one in-root package-lock.json using only a prepared local database."""
    result: dict[str, Any] = {
        "id": "npm-advisories",
        "status": "error",
        "summary": "Offline advisory check failed; verify the lockfile and local database.",
        "findings": [],
        "coverage": {
            "tool": "osv-scanner",
            "scope": "Explicit npm lockfile against prepared local advisories only; database freshness is not verified.",
            "offline": True,
        },
    }
    try:
        root = root.resolve()
        target = (root / lockfile).resolve()
        if not target.is_relative_to(root) or target.name != "package-lock.json":
            result["summary"] = "Specify a package-lock.json inside the project root."
            return result
        relative = target.relative_to(root).as_posix()
        # Do not emit terminal controls from a caller-controlled filename.
        if any(ord(char) < 32 or ord(char) == 127 for char in relative):
            return result
        result["coverage"]["inputs"] = [relative]
        if not target.is_file():
            result.update(
                status="unavailable", summary="The requested npm lockfile is missing."
            )
            return result
        database = (root / database).resolve()
        if not (database / "osv-scalibr" / "npm" / "all.zip").is_file():
            result.update(
                status="unavailable",
                summary="Prepare osv-scalibr/npm/all.zip in the selected offline cache root.",
            )
            return result
        binary = (
            str(executable) if executable is not None else shutil.which("osv-scanner")
        )
        if binary is None:
            result.update(
                status="unavailable",
                summary="OSV-Scanner v2.7+ is not installed on PATH.",
            )
            return result
        env = dict(
            os.environ,
            OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY=str(database),
            OSV_SCALIBR_LOCAL_DB_CACHE_DIRECTORY=str(database),
        )
        code, output = _run([binary, "--version"], root, env)
        match = re.search(r"(?m)^osv-scanner version: (2\.\d+\.\d+)\s*$", output)
        if code != 0 or match is None or int(match[1].split(".")[1]) < 7:
            result.update(
                status="unavailable",
                summary="Use OSV-Scanner v2.7 or newer within v2 for package source coverage.",
            )
            return result
        result["coverage"]["version"] = match[1]
        # An explicit empty config prevents local IgnoreVulns/PackageOverrides
        # from silently hiding advisories in this independent check.
        with tempfile.TemporaryDirectory(prefix="sentinel-osv-") as temporary:
            config = Path(temporary) / "osv-scanner.toml"
            config.write_text("", encoding="utf-8")
            code, output = _run(
                [
                    binary,
                    "scan",
                    "source",
                    "--offline",
                    "--no-resolve",
                    "--no-call-analysis=all",
                    "--all-packages",
                    "--format=json",
                    f"--config={config}",
                    f"--lockfile=:{target}",
                ],
                root,
                env,
            )
        if code == 128:
            result.update(
                status="unavailable", summary="OSV-Scanner found no packages to check."
            )
            return result
        if code not in (0, 1):
            return result
        findings, count, unassessed = _parse(output, target, root)
        if count == 0:
            result.update(
                status="unavailable",
                summary="OSV-Scanner returned no package coverage.",
            )
            return result
        if not unassessed and bool(findings) != (code == 1):
            return result
        result["coverage"]["packages"] = count
        result["coverage"]["assessed_packages"] = count - unassessed
        result["coverage"]["unassessed_packages"] = unassessed
        if unassessed:
            result.update(
                status="unavailable",
                summary=(
                    f"{unassessed} locked package(s) lack public-registry source or "
                    "valid resolved-version evidence. Advisory coverage is incomplete."
                ),
                findings=findings,
            )
            return result
        result.update(
            status="findings" if findings else "complete",
            summary=(
                "Local OSV advisories match locked npm packages."
                if findings
                else "No matching advisories in the prepared local npm database."
            ),
            findings=findings,
        )
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        # Diagnostics can contain lockfile contents, paths, or credentials.
        # The fixed error summary deliberately contains none of those values.
        return result
    return result
