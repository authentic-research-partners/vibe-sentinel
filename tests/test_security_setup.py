"""Managed resources must stay offline at use time and survive failed refreshes."""

import base64
from email.utils import formatdate
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time
from typing import Any
from zipfile import ZipFile

import httpx
import pytest

from vibe_sentinel import security_setup as managed


@pytest.fixture
def provisioner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Public resources are synthetic; manifest, integrity and promotion paths are real."""
    binary = b"synthetic executable OSV 2.7.0"
    node = tmp_path / "system-node"
    node.write_text("synthetic trusted Node.js runtime")
    node.chmod(0o700)
    monkeypatch.setattr(managed, "_node", lambda: node)
    monkeypatch.setattr(managed, "_platform", lambda: "linux_amd64")
    monkeypatch.setitem(
        managed.OSV_DIGESTS, "linux_amd64", hashlib.sha256(binary).hexdigest()
    )
    monkeypatch.setattr(managed.shutil, "which", lambda *args, **kwargs: "/system/uv")
    archive = io.BytesIO()
    with ZipFile(archive, "w") as compressed:
        compressed.writestr(
            "GHSA-test-test-test.json", '{"id":"GHSA-test-test-test","affected":[]}'
        )
    package = io.BytesIO()
    with tarfile.open(fileobj=package, mode="w:gz") as tar:
        for name, content in (
            ("package/lib/typescript.js", b"module.exports = {};"),
            ("package/LICENSE.txt", b"Apache-2.0"),
        ):
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            tar.addfile(entry, io.BytesIO(content))
    monkeypatch.setattr(
        managed,
        "TYPESCRIPT_SHA512",
        base64.b64encode(hashlib.sha512(package.getvalue()).digest()).decode(),
    )
    calls: dict[str, Any] = {
        "downloads": [],
        "commands": [],
        "root": tmp_path / "managed",
        "node": node,
    }

    def download(url: str, target: Path, limit: int, root: Path) -> dict[str, Any]:
        calls["downloads"].append(url)
        content = (
            archive.getvalue()
            if url == managed.ADVISORY_URL
            else package.getvalue()
            if url == managed.TYPESCRIPT_URL
            else binary
        )
        target.write_bytes(content)
        return {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "last_modified": formatdate(time.time(), usegmt=True),
        }

    def command(argv: list[str], directory: Path, root: Path) -> None:
        calls["commands"].append(argv)
        if "install" not in argv:
            return
        environment = directory / "semgrep"
        for relative, content in (
            ("bin/semgrep", "#!/synthetic/python\n"),
            ("bin/python", "synthetic python"),
            ("lib/python3.13/site-packages/semgrep/bin/semgrep-core", "synthetic core"),
            ("lib/python3.13/site-packages/yaml/__init__.py", "synthetic dependency"),
            (
                f"lib/python3.13/site-packages/semgrep-{managed.SEMGREP_VERSION}.dist-info/METADATA",
                f"Name: semgrep\nVersion: {managed.SEMGREP_VERSION}\n",
            ),
        ):
            path = environment / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            path.chmod(0o700)

    monkeypatch.setattr(managed, "_download", download)
    monkeypatch.setattr(managed, "_install_command", command)
    return calls


def test_unconfigured_status_and_resolve_are_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("status must not access network or run a process")

    monkeypatch.setattr(managed.httpx, "Client", forbidden)
    monkeypatch.setattr(managed.subprocess, "Popen", forbidden)
    root = tmp_path / "absent"
    assert managed.status(root)["status"] == "unavailable"
    assert managed.resolve(root) == {}
    assert not root.exists()


def test_setup_pins_and_isolates_resources_and_is_idempotent(
    provisioner: dict[str, Any],
) -> None:
    root = provisioner["root"]
    assert managed.setup(root)["status"] == "ready"
    paths = managed.resolve(root)
    assert set(paths) == {"osv", "semgrep", "typescript", "node", "advisory_db"}
    assert paths["node"] == provisioner["node"]
    assert all(
        path.is_relative_to(root) for key, path in paths.items() if key != "node"
    )
    assert (paths["advisory_db"] / "osv-scalibr/npm/all.zip").is_file()
    commands = provisioner["commands"]
    assert all(
        "--no-config" in argv
        and "--no-cache" in argv
        and "--no-python-downloads" in argv
        for argv in commands
    )
    assert f"semgrep=={managed.SEMGREP_VERSION}" in commands[1]
    assert "--only-binary" in commands[1]
    assert "https://pypi.org/simple" in commands[1]
    downloads = list(provisioner["downloads"])
    assert managed.setup(root)["ready"]
    assert provisioner["downloads"] == downloads
    assert managed.resolve(root) == paths


def test_refresh_reuses_tools_and_keeps_previous_snapshot(
    provisioner: dict[str, Any],
) -> None:
    root = provisioner["root"]
    managed.setup(root)
    before = managed.resolve(root)
    count = len(provisioner["commands"])
    assert managed.setup(root, refresh=True)["status"] == "ready"
    after = managed.resolve(root)
    assert before["osv"] == after["osv"]
    assert before["semgrep"] == after["semgrep"]
    assert before["advisory_db"] != after["advisory_db"]
    assert before["advisory_db"].is_dir()
    assert len(provisioner["commands"]) == count
    assert provisioner["downloads"][-1] == managed.ADVISORY_URL


def test_failed_refresh_retains_last_good_and_records_fixed_error(
    provisioner: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = provisioner["root"]
    managed.setup(root)
    before = managed.resolve(root)
    pointer = (root / "current.json").read_bytes()

    monkeypatch.setattr(
        managed,
        "_download",
        lambda *args: (_ for _ in ()).throw(managed._SetupError("download")),
    )
    result = managed.setup(root, refresh=True)
    assert result["status"] == "error" and result["ready"]
    assert result["error_code"] == "download"
    assert managed.resolve(root) == before
    assert (root / "current.json").read_bytes() == pointer
    assert managed.status(root)["last_setup"]["status"] == "error"
    assert len(list((root / "advisories").iterdir())) == 1


def test_interrupted_setup_does_not_promote_partial_resources(
    provisioner: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = provisioner["root"]

    def interrupt(*args: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(managed, "_install_advisories", interrupt)
    with pytest.raises(KeyboardInterrupt):
        managed.setup(root)
    assert not (root / "current.json").exists()
    assert managed.resolve(root) == {}
    assert not list((root / "tools").iterdir())


def test_corrupt_binary_does_not_hide_independent_database(
    provisioner: dict[str, Any],
) -> None:
    root = provisioner["root"]
    managed.setup(root)
    paths = managed.resolve(root)
    paths["osv"].write_bytes(b"different binary")
    resolved = managed.resolve(root)
    assert "osv" not in resolved
    assert resolved["advisory_db"] == paths["advisory_db"]
    assert "semgrep" in resolved
    assert managed.status(root)["status"] == "unavailable"


def test_corrupt_database_is_not_resolved(provisioner: dict[str, Any]) -> None:
    root = provisioner["root"]
    managed.setup(root)
    archive = managed.resolve(root)["advisory_db"] / "osv-scalibr/npm/all.zip"
    original = archive.read_bytes()
    archive.write_bytes(b"X" + original[1:])
    assert "advisory_db" not in managed.resolve(root)


def test_stale_snapshot_is_explicit_and_never_auto_refreshes(
    provisioner: dict[str, Any],
) -> None:
    root = provisioner["root"]
    managed.setup(root)
    database = managed.resolve(root)["advisory_db"]
    manifest = managed._read_json(database / "manifest.json")
    manifest["source_modified_at"] = time.time() - managed.STALE_SECONDS - 3600
    managed._write_json(database / "manifest.json", manifest)
    calls = list(provisioner["downloads"])
    report = managed.status(root)
    assert report["status"] == "stale" and report["ready"]
    assert managed.resolve(root)["advisory_db"] == database
    assert provisioner["downloads"] == calls


@pytest.mark.parametrize(
    "bad_pointer",
    [
        {"schema_version": 1, "tools": "../../outside", "advisories": []},
        {"schema_version": 9, "tools": "a" * 32},
        [],
    ],
)
def test_invalid_pointer_never_escapes_managed_directory(
    tmp_path: Path, bad_pointer: Any
) -> None:
    (tmp_path / "current.json").write_text(json.dumps(bad_pointer))
    assert managed.resolve(tmp_path) == {}


def test_storage_limit_preserves_current_resources(
    provisioner: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = provisioner["root"]
    managed.setup(root)
    before = managed.resolve(root)
    monkeypatch.setattr(managed, "MAX_STORAGE", 1)
    assert managed.setup(root, refresh=True)["error_code"] == "storage"
    assert managed.resolve(root) == before


def test_cross_process_lock_rejects_second_setup(tmp_path: Path) -> None:
    import fcntl

    with (tmp_path / "setup.lock").open("w") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; from vibe_sentinel.security_setup import setup; sys.exit(0 if setup(Path(sys.argv[1])).get('error_code') == 'busy' else 1)",
                str(tmp_path),
            ],
            capture_output=True,
            timeout=10,
        )
    assert child.returncode == 0
    assert not (tmp_path / "current.json").exists()


@pytest.mark.parametrize(
    "name,body",
    [("../escape.json", b"{}"), ("ok.json", b"invalid compressed contents")],
)
def test_advisory_archive_validation_rejects_unsafe_or_corrupt_zip(
    tmp_path: Path, name: str, body: bytes
) -> None:
    archive = tmp_path / "all.zip"
    if name == "ok.json":
        archive.write_bytes(body)
    else:
        with ZipFile(archive, "w") as compressed:
            compressed.writestr(name, body)
    with pytest.raises(managed._SetupError, match="archive"):
        managed._validate_archive(archive)


def test_download_size_and_remote_errors_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"overlong body")
    )
    monkeypatch.setattr(
        managed.httpx,
        "Client",
        lambda **kwargs: original(transport=transport, **kwargs),
    )
    with pytest.raises(managed._SetupError, match="download"):
        managed._download(
            "https://example.test/public", tmp_path / "download", 2, tmp_path
        )
    assert (tmp_path / "download").stat().st_size == 0


def test_installer_environment_discards_project_and_user_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV_INDEX", "https://private.invalid")
    monkeypatch.setenv("PIP_INDEX_URL", "https://private.invalid")
    monkeypatch.setenv("PYTHONPATH", "/project/injected")
    script = "import json, os; from pathlib import Path; Path('seen.json').write_text(json.dumps(dict(os.environ)))"
    managed._install_command([sys.executable, "-I", "-c", script], tmp_path, tmp_path)
    environment = json.loads((tmp_path / "seen.json").read_text())
    assert (
        not {"UV_INDEX", "PIP_INDEX_URL", "PYTHONPATH", "VIRTUAL_ENV"}
        & environment.keys()
    )
    assert environment["HOME"] == str(tmp_path)
    assert environment["PATH"] == os.defpath


def test_installer_timeout_terminates_process_and_reports_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(managed, "TIMEOUT", 0.01)
    started = time.monotonic()
    with pytest.raises(managed._SetupError, match="install"):
        managed._install_command(
            [sys.executable, "-c", "import time; print('PRIVATE'); time.sleep(5)"],
            tmp_path,
            tmp_path,
        )
    assert time.monotonic() - started < 3


def test_changed_node_does_not_disable_osv(provisioner: dict[str, Any]) -> None:
    root = provisioner["root"]
    managed.setup(root)
    provisioner["node"].write_text("a different Node.js installation")
    result = managed.status(root)
    assert result["components"]["node"]["status"] == "unavailable"
    assert "osv" in managed.resolve(root)


@pytest.mark.parametrize(
    "resource,error", [("osv", "checksum"), ("typescript", "typescript")]
)
def test_downloaded_tools_must_match_pinned_integrity(
    provisioner: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    resource: str,
    error: str,
) -> None:
    if resource == "osv":
        monkeypatch.setitem(managed.OSV_DIGESTS, "linux_amd64", "0" * 64)
    else:
        monkeypatch.setattr(managed, "TYPESCRIPT_SHA512", "invalid expected digest")
    root = provisioner["root"]
    result = managed.setup(root)
    assert result["error_code"] == error
    assert not result["ready"]
    assert not (root / "current.json").exists()
    assert not list((root / "tools").iterdir())


def test_network_error_body_is_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(403, text="PRIVATE REMOTE ERROR")
    )
    monkeypatch.setattr(
        managed.httpx,
        "Client",
        lambda **kwargs: original(transport=transport, **kwargs),
    )
    with pytest.raises(managed._SetupError) as error:
        managed._download(managed.ADVISORY_URL, tmp_path / "download", 10, tmp_path)
    assert str(error.value) == "download"


def test_missing_node_is_explicit_and_performs_no_download(
    provisioner: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing() -> Path:
        raise managed._SetupError("node")

    monkeypatch.setattr(managed, "_node", missing)
    result = managed.setup(provisioner["root"])
    assert result["error_code"] == "node"
    assert "Install Node.js" in result["diagnostic"]
    assert not provisioner["downloads"]


def test_status_does_not_read_manifest_fifo(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "current.json")
    assert managed.status(tmp_path)["status"] == "unavailable"


@pytest.mark.parametrize(
    "damage",
    [
        "remove_core",
        "change_core",
        "empty_python",
        "nonexecutable_python",
        "remove_package",
        "remove_dependency",
        "change_dependency",
    ],
)
def test_semgrep_runtime_damage_is_unavailable_and_setup_repairs(
    provisioner: dict[str, Any], damage: str
) -> None:
    root = provisioner["root"]
    managed.setup(root)
    original = managed.resolve(root)["semgrep"]
    environment = original.parent.parent
    core = environment / "lib/python3.13/site-packages/semgrep/bin/semgrep-core"
    interpreter = environment / "bin/python"
    if damage == "remove_core":
        core.unlink()
    elif damage == "change_core":
        core.write_text("modified executable")
    elif damage == "empty_python":
        interpreter.write_bytes(b"")
    elif damage == "nonexecutable_python":
        interpreter.chmod(0o600)
    elif damage in {"remove_dependency", "change_dependency"}:
        dependency = environment / "lib/python3.13/site-packages/yaml/__init__.py"
        if damage == "remove_dependency":
            dependency.unlink()
        else:
            dependency.write_text("altered dependency")
    else:
        managed.shutil.rmtree(core.parent.parent)
    assert "semgrep" not in managed.resolve(root)
    assert managed.setup(root)["status"] == "ready"
    assert managed.resolve(root)["semgrep"] != original


def test_generated_bytecode_does_not_invalidate_semgrep(
    provisioner: dict[str, Any],
) -> None:
    root = provisioner["root"]
    managed.setup(root)
    launcher = managed.resolve(root)["semgrep"]
    cache = launcher.parent.parent / "lib/python3.13/site-packages/semgrep/__pycache__"
    cache.mkdir()
    (cache / "generated.pyc").write_bytes(b"runtime bytecode")
    assert managed.resolve(root)["semgrep"] == launcher


def test_retry_cleans_only_uncommitted_staging(provisioner: dict[str, Any]) -> None:
    root = provisioner["root"]
    managed.setup(root)
    first = managed.resolve(root)
    managed.setup(root, refresh=True)
    current = managed.resolve(root)
    abandoned = root / "advisories" / ("a" * 32)
    abandoned.mkdir()
    (abandoned / ".pending").touch()
    (abandoned / "partial.zip").write_bytes(b"interrupted download")
    # A crash after pointer promotion but before marker removal must preserve selection.
    (current["advisory_db"] / ".pending").touch()
    assert managed.setup(root)["status"] == "ready"
    assert not abandoned.exists()
    assert first["advisory_db"].is_dir()
    assert current["advisory_db"].is_dir()
    assert not (current["advisory_db"] / ".pending").exists()
    assert managed.resolve(root) == current


def test_interruption_after_pointer_publication_keeps_selected_resources(
    provisioner: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = provisioner["root"]
    original_write = managed._write_json

    def interrupted_write(path: Path, value: dict[str, Any]) -> None:
        original_write(path, value)
        if path.name == "current.json":
            raise KeyboardInterrupt

    monkeypatch.setattr(managed, "_write_json", interrupted_write)
    with pytest.raises(KeyboardInterrupt):
        managed.setup(root)
    assert managed.status(root)["ready"]
    selected = managed.resolve(root)
    assert set(selected) == {"osv", "semgrep", "typescript", "node", "advisory_db"}
    assert selected["osv"].is_file()
    assert selected["semgrep"].is_file()
    assert (selected["advisory_db"] / "osv-scalibr/npm/all.zip").is_file()
    monkeypatch.setattr(managed, "_write_json", original_write)
    assert managed.setup(root)["ready"]
    assert managed.resolve(root) == selected
