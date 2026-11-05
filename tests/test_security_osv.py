"""Offline adapter contract, using synthetic OSV v2 machine responses."""

import json
from pathlib import Path
import subprocess
import shutil
from typing import Any
from zipfile import ZipFile

import pytest

from vibe_sentinel import security_osv


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {"node_modules/lodash": {"version": "4.17.20"}},
            }
        )
    )
    database = tmp_path / "database"
    archive = database / "osv-scalibr/npm/all.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"synthetic database; subprocess is stubbed")
    return tmp_path, database


def response(root: Path, vulnerable: bool = True) -> dict[str, Any]:
    return {
        "results": [
            {
                "source": {"path": str(root / "package-lock.json"), "type": "lockfile"},
                "packages": [
                    {
                        "package": {
                            "name": "lodash",
                            "version": "4.17.20" if vulnerable else "4.17.21",
                            "ecosystem": "npm",
                            "purl": (
                                "pkg:npm/lodash@"
                                + ("4.17.20" if vulnerable else "4.17.21")
                                + "?source=PUBLIC_REGISTRY"
                            ),
                        },
                        **(
                            {
                                "vulnerabilities": [
                                    {
                                        "id": "GHSA-35jh-r3h4-6jhm",
                                        "details": "PRIVATE RAW CONTENT",
                                    }
                                ]
                            }
                            if vulnerable
                            else {}
                        ),
                    }
                ],
            }
        ],
    }


def fake_tool(
    monkeypatch: pytest.MonkeyPatch, payload: Any, code: int = 1, version: str = "2.7.0"
) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(security_osv.shutil, "which", lambda _: "/usr/bin/osv-scanner")

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        assert kwargs["timeout"] == 120
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["stdin"] == subprocess.DEVNULL
        if argv[-1] == "--version":
            output = f"osv-scanner version: {version}\ncommit: unused\n"
            result_code = 0
        else:
            assert "--offline" in argv
            assert "--all-packages" in argv
            assert "--no-call-analysis=all" in argv
            assert "--no-resolve" in argv
            assert not any("download" in arg for arg in argv)
            assert kwargs["env"]["OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY"]
            assert (
                kwargs["env"]["OSV_SCALIBR_LOCAL_DB_CACHE_DIRECTORY"]
                == kwargs["env"]["OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY"]
            )
            config = next(
                arg.split("=", 1)[1] for arg in argv if arg.startswith("--config=")
            )
            assert Path(config).read_text() == ""
            output = payload if isinstance(payload, str) else json.dumps(payload)
            result_code = code
        kwargs["stdout"].write(output.encode())
        return subprocess.CompletedProcess(argv, result_code)

    monkeypatch.setattr(security_osv.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("vulnerable", [True, False])
def test_vulnerable_and_patched(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, vulnerable: bool
) -> None:
    root, database = project
    monkeypatch.setenv("OSV_SCALIBR_LOCAL_DB_CACHE_DIRECTORY", "/ambient-cache")
    fake_tool(monkeypatch, response(root, vulnerable), int(vulnerable))
    result = security_osv.check(root, "package-lock.json", database)
    assert result["status"] == ("findings" if vulnerable else "complete")
    assert result["coverage"]["packages"] == 1
    assert result["coverage"]["version"] == "2.7.0"
    assert "PRIVATE" not in json.dumps(result)
    if vulnerable:
        finding = result["findings"][0]
        assert finding["path"] == "package-lock.json"
        assert finding["advisory"] == "GHSA-35jh-r3h4-6jhm"
        assert (
            security_osv.check(root, "package-lock.json", database)["findings"]
            == result["findings"]
        )


@pytest.mark.parametrize(
    "payload,code,status",
    [
        ({"results": []}, 0, "unavailable"),
        ({}, 0, "error"),
        ({"results": None}, 0, "error"),
        ({"results": [None]}, 0, "error"),
        ("private invalid output", 0, "error"),
        ("private error", 127, "error"),
        ("no packages", 128, "unavailable"),
        ("private error", 129, "error"),
    ],
)
def test_incomplete_is_not_clean(
    project: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    payload: Any,
    code: int,
    status: str,
) -> None:
    root, database = project
    fake_tool(monkeypatch, payload, code)
    result = security_osv.check(root, "package-lock.json", database)
    assert result["status"] == status
    assert result["findings"] == []
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("missing", ["binary", "lockfile", "database"])
def test_missing_prerequisite(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    root, database = project
    monkeypatch.setattr(
        security_osv.shutil,
        "which",
        lambda _: None if missing == "binary" else "/unused",
    )
    if missing == "lockfile":
        (root / "package-lock.json").unlink()
    if missing == "database":
        database = root / "missing"
    assert (
        security_osv.check(root, "package-lock.json", database)["status"]
        == "unavailable"
    )


@pytest.mark.parametrize(
    "version", ["1.9.2", "2.4.0", "2.5.0", "2.5.1", "2.6.0", "3.0.0", "private-version"]
)
def test_unsupported_version(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    root, database = project
    calls = fake_tool(monkeypatch, {}, version=version)
    assert (
        security_osv.check(root, "package-lock.json", database)["status"]
        == "unavailable"
    )
    assert len(calls) == 1


def test_path_escape_and_symlink(project: tuple[Path, Path], tmp_path: Path) -> None:
    root, database = project
    assert (
        security_osv.check(root, "../package-lock.json", database)["status"] == "error"
    )
    (root / "package-lock.json").unlink()
    (root / "package-lock.json").symlink_to(tmp_path.parent / "package-lock.json")
    assert security_osv.check(root, "package-lock.json", database)["status"] == "error"


@pytest.mark.parametrize("damage", ["source", "name", "advisory", "exit", "packages"])
def test_untrusted_machine_metadata(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    root, database = project
    payload = response(root)
    source = payload["results"][0]
    if damage == "source":
        source["source"]["path"] = "/private/package-lock.json"
    elif damage == "name":
        source["packages"][0]["package"]["name"] = "private\ncontent"
    elif damage == "advisory":
        source["packages"][0]["vulnerabilities"][0]["id"] = "private\x1bcontent"
    elif damage == "packages":
        source["packages"] = None
    fake_tool(monkeypatch, payload, 0 if damage == "exit" else 1)
    result = security_osv.check(root, "package-lock.json", database)
    assert result["status"] == "error"
    assert "private" not in json.dumps(result)


def test_malware_advisory_is_retained(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database = project
    payload = response(root)
    payload["results"][0]["packages"][0]["vulnerabilities"][0]["id"] = "MAL-2026-1330"
    fake_tool(monkeypatch, payload, 1)
    result = security_osv.check(root, "package-lock.json", database)
    assert result["status"] == "findings"
    assert result["findings"][0]["advisory"] == "MAL-2026-1330"


@pytest.mark.parametrize(
    ("purl", "version"),
    [
        ("pkg:npm/lodash@4.17.20?source=LOCAL", "4.17.20"),
        ("pkg:npm/lodash@4.17.20", "4.17.20"),
        (None, "4.17.20"),
        ("pkg:npm/lodash@not-a-version?source=PUBLIC_REGISTRY", "not-a-version"),
        ("pkg:npm/lodash@01.2.3?source=PUBLIC_REGISTRY", "01.2.3"),
        ("pkg:npm/lodash@1.2.3-alpha.01?source=PUBLIC_REGISTRY", "1.2.3-alpha.01"),
    ],
)
def test_unassessed_package_retains_assessed_sibling_findings(
    project: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    purl: str | None,
    version: str,
) -> None:
    root, database = project
    data = response(root)
    data["results"][0]["packages"].append(
        {
            "package": {
                "name": "unassessed",
                "version": version,
                "ecosystem": "npm",
                "purl": purl,
            }
        }
    )
    fake_tool(monkeypatch, data, 1)
    result = security_osv.check(root, "package-lock.json", database)
    assert result["status"] == "unavailable"
    assert len(result["findings"]) == 1
    assert result["coverage"]["assessed_packages"] == 1
    assert result["coverage"]["unassessed_packages"] == 1


@pytest.mark.parametrize(
    "version", ["1.2.3-beta.1+build.001", "v1.2.3", "0.0.0", "1.2.3-0"]
)
def test_supported_resolved_versions(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    root, database = project
    data = response(root, False)
    data["results"][0]["packages"][0]["package"]["version"] = version
    fake_tool(monkeypatch, data, 0)
    result = security_osv.check(root, "package-lock.json", database)
    assert result["status"] == "complete"
    assert result["coverage"]["unassessed_packages"] == 0


def test_timeout_hides_diagnostics(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database = project
    monkeypatch.setattr(security_osv.shutil, "which", lambda _: "/unused")

    def timeout(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired("private command", 120, output="private output")

    monkeypatch.setattr(security_osv.subprocess, "run", timeout)
    result = security_osv.check(root, "package-lock.json", database)
    assert result["status"] == "error"
    assert "private" not in json.dumps(result)


def test_current_cache_layout_and_relative_source(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database = project
    payload = response(root, False)
    payload["results"][0]["source"]["path"] = "package-lock.json"
    fake_tool(monkeypatch, payload, 0, version="2.7.0")
    assert (
        security_osv.check(root, "package-lock.json", database)["status"] == "complete"
    )


def test_output_size_limit(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database = project
    monkeypatch.setattr(security_osv, "_OUTPUT_LIMIT", 128)
    fake_tool(monkeypatch, "private" * 100)
    result = security_osv.check(root, "package-lock.json", database)
    assert result["status"] == "error"
    assert "private" not in json.dumps(result)


@pytest.mark.skipif(
    shutil.which("osv-scanner") is None, reason="OSV-Scanner is not installed"
)
@pytest.mark.parametrize(
    ("version", "resolved", "mixed", "status"),
    [
        ("1.0.0", True, False, "findings"),
        ("1.0.1", True, False, "complete"),
        ("1.0.0", False, False, "unavailable"),
        ("1.0.0", True, True, "unavailable"),
    ],
)
def test_real_offline_scanner_with_local_advisory(
    tmp_path: Path, version: str, resolved: bool, mixed: bool, status: str
) -> None:
    """The real scanner must extract and match versions, without network data."""
    advisory = {
        "schema_version": "1.7.0",
        "id": "OSV-SENTINEL-TEST",
        "modified": "2026-11-05T00:00:00Z",
        "affected": [
            {
                "package": {"ecosystem": "npm", "name": "sentinel-fixture"},
                "ranges": [
                    {
                        "type": "SEMVER",
                        "events": [{"introduced": "0"}, {"fixed": "1.0.1"}],
                    }
                ],
            }
        ],
    }
    database = tmp_path / "database"
    for layout in ("osv-scanner", "osv-scalibr"):
        archive = database / layout / "npm" / "all.zip"
        archive.parent.mkdir(parents=True)
        with ZipFile(archive, "w") as zipped:
            zipped.writestr("OSV-SENTINEL-TEST.json", json.dumps(advisory))
    package: dict[str, Any] = {"version": version}
    if resolved:
        package["resolved"] = (
            "https://registry.npmjs.org/sentinel-fixture/-/"
            f"sentinel-fixture-{version}.tgz"
        )
    lock: dict[str, Any] = {
        "name": "sentinel-fixture-project",
        "version": "1.0.0",
        "lockfileVersion": 3,
        "packages": {
            "": {"dependencies": {"sentinel-fixture": version}},
            "node_modules/sentinel-fixture": package,
        },
    }
    if mixed:
        lock["packages"]["node_modules/local-fixture"] = {"version": "1.0.0"}
    (tmp_path / "package-lock.json").write_text(json.dumps(lock))
    result = security_osv.check(tmp_path, "package-lock.json", database)
    assert result["status"] == status, result["summary"]
    assert result["coverage"]["packages"] == 1 + int(mixed)
    assert result["coverage"]["offline"] is True
    assert [finding["advisory"] for finding in result["findings"]] == (
        ["OSV-SENTINEL-TEST"] if resolved and version == "1.0.0" else []
    )
