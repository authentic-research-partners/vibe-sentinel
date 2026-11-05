"""Explicit provisioning of isolated scanners and public offline advisories.

Only setup performs network or subprocess work. Immutable tool and database
generations are selected together by an atomic pointer; refresh preserves the
last working selection. Committed generations are retained because a concurrent
scan may still hold their paths. The storage ceiling bounds that retention.
"""

from __future__ import annotations

from contextlib import contextmanager
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from typing import Any, Iterator
from uuid import uuid4
from zipfile import BadZipFile, ZipFile
import zlib

import httpx

from vibe_sentinel.log import logger


OSV_VERSION = "2.7.0"
SEMGREP_VERSION = "1.183.0"
TYPESCRIPT_VERSION = "5.8.3"
TYPESCRIPT_URL = "https://registry.npmjs.org/typescript/-/typescript-5.8.3.tgz"
TYPESCRIPT_SHA512 = "p1diW6TqL9L07nNxvRMM7hMMw4c5XOo/1ibL4aAIGmSAt9slTE1Xgw5KWuof2uTOvCg9BY7ZRi+GaF+7sfgPeQ=="
# https://github.com/google/osv-scanner/releases/download/v2.7.0/osv-scanner_SHA256SUMS
OSV_DIGESTS = {
    "darwin_amd64": "e82c5df7ab0a42dff29a1cd0660711447b56b4c0483ff66ad8634df10fd81a45",
    "darwin_arm64": "d4926ef8da134332543be276dff157e4c170f99ad91fc48e89448a8816bdf70b",
    "linux_amd64": "160c6253e2922e9e0a6a9771c5c4c40a4b3c6090b34a3cdd5e80c96cc2e4218b",
    "linux_arm64": "4061ef4c021ed3c47f203b818f337e351336cc5cd67c0274fc36bd1dfc728dd7",
}
ADVISORY_URL = "https://osv-vulnerabilities.storage.googleapis.com/npm/all.zip"
MAX_STORAGE = 4 * 1024**3
MAX_BINARY = 200 * 1024**2
MAX_ARCHIVE = 512 * 1024**2
MAX_EXPANDED = 2 * 1024**3
TIMEOUT = 300
STALE_SECONDS = 7 * 24 * 3600
_REMEDIATION = "Run vibe-sentinel security setup --refresh."
_DIAGNOSTICS = {
    "download": "A public security resource could not be downloaded or exceeded its limit.",
    "checksum": "The OSV binary did not match its pinned release checksum.",
    "archive": "The public advisory archive failed validation.",
    "metadata": "The public advisory snapshot metadata is missing or invalid.",
    "install": "The isolated scanner installation failed or exceeded its limit.",
    "uv": "Install uv before running vibe-sentinel security setup.",
    "platform": "Managed security tools support macOS and Linux on arm64 or x86_64.",
    "storage": "The managed security directory reached its 4 GiB limit or lacks free space.",
    "busy": "Another security setup is running. Wait for it to finish and retry.",
    "filesystem": "The managed security directory could not be read or written.",
    "validation": "The installed security resources failed validation.",
    "node": "Install Node.js in a system location before running vibe-sentinel security setup.",
    "typescript": "The pinned TypeScript parser archive failed validation.",
}


class _SetupError(Exception):
    """Only fixed codes, never remote responses or installer diagnostics."""


def default_directory() -> Path:
    return Path.home() / ".cache" / "vibe-sentinel" / "security"


def _directory(directory: Path | None) -> Path:
    return (directory if directory is not None else default_directory()).resolve()


def _platform() -> str:
    machine = {"x86_64": "amd64", "aarch64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    return f"{platform.system().lower()}_{machine}"


def _read_json(path: Path) -> dict[str, Any]:
    # Preserve FileNotFoundError so cleanup can distinguish no selection from an
    # invalid selection whose resources must be retained conservatively.
    size = path.stat().st_size
    if path.is_symlink() or not path.is_file() or size > 32 * 1024:
        raise ValueError("invalid manifest")
    with path.open("rb") as stream:
        value = json.loads(stream.read(32 * 1024 + 1))
    if not isinstance(value, dict):
        raise ValueError("invalid manifest")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}-{uuid4().hex}")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _digest(path: Path, limit: int) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("invalid resource")
    result = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise ValueError("resource exceeded limit")
            result.update(chunk)
    return result.hexdigest()


def _runtime_digest(directory: Path) -> str:
    """Check the isolated runtime, including dependencies, excluding generated bytecode."""
    digest = hashlib.sha256()
    size = 0
    count = 0
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("invalid installed package")
    for parent, directories, filenames in os.walk(directory, followlinks=False):
        directories[:] = sorted(name for name in directories if name != "__pycache__")
        if any((Path(parent) / name).is_symlink() for name in directories):
            raise ValueError("invalid installed package directory")
        for name in sorted(filenames):
            if name.endswith(".pyc"):
                continue
            path = Path(parent) / name
            count += 1
            size += path.stat().st_size
            if count > 10_000 or size > 512 * 1024**2:
                raise ValueError("installed package exceeds limits")
            digest.update(path.relative_to(directory).as_posix().encode())
            digest.update(bytes.fromhex(_digest(path, 512 * 1024**2)))
    if not count:
        raise ValueError("empty installed package")
    return digest.hexdigest()


def _generation(root: Path, group: str, identifier: Any) -> Path:
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise ValueError("invalid generation")
    result = root / group / identifier
    if result.resolve() != result.absolute() or not result.is_dir():
        raise ValueError("invalid generation directory")
    return result


def _component(path: Path, version: str) -> dict[str, Any]:
    return {"status": "ready", "path": str(path), "version": version}


def _node() -> Path:
    # A project-controlled PATH or node_modules must never supply our parser host.
    executable = shutil.which(
        "node", path="/opt/homebrew/bin:/usr/local/bin:" + os.defpath
    )
    if executable is None:
        raise _SetupError("node")
    result = Path(executable).resolve()
    if "node_modules" in result.parts:
        raise _SetupError("node")
    return result


def _tools_status(directory: Path) -> dict[str, dict[str, Any]]:
    manifest = _read_json(directory / "manifest.json")
    if manifest.get("platform") != _platform():
        raise ValueError("different platform")
    result = {}
    for key, validate in (
        ("osv", _osv_status),
        ("semgrep", _semgrep_status),
        ("typescript", _typescript_status),
        ("node", _node_status),
    ):
        try:
            result[key] = validate(directory, manifest)
        except (OSError, ValueError, _SetupError):
            result[key] = {
                "status": "unavailable",
                "diagnostic": _DIAGNOSTICS["node"] if key == "node" else _REMEDIATION,
            }
    return result


def _osv_status(directory: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    binary = directory / "osv-scanner"
    expected = OSV_DIGESTS.get(_platform())
    if (
        expected is None
        or manifest.get("osv_version") != OSV_VERSION
        or _digest(binary, MAX_BINARY) != expected
        or not os.access(binary, os.X_OK)
    ):
        raise ValueError("invalid OSV installation")
    return _component(binary, OSV_VERSION)


def _semgrep_status(directory: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    launcher = directory / "semgrep" / "bin" / "semgrep"
    metadata = list(
        (directory / "semgrep" / "lib").glob(
            "python*/site-packages/semgrep-*.dist-info/METADATA"
        )
    )
    if (
        manifest.get("semgrep_version") != SEMGREP_VERSION
        or len(metadata) != 1
        or not metadata[0].is_file()
        or metadata[0].stat().st_size > 1024 * 1024
        or f"\nVersion: {SEMGREP_VERSION}\n" not in metadata[0].read_text()
        or not os.access(launcher, os.X_OK)
        or _digest(launcher, 1024 * 1024) != manifest.get("semgrep_launcher_sha256")
        or not (directory / "semgrep" / "bin" / "python").is_file()
        or not (metadata[0].parent.parent / "semgrep/bin/semgrep-core").is_file()
        or not os.access(
            metadata[0].parent.parent / "semgrep/bin/semgrep-core", os.X_OK
        )
    ):
        raise ValueError("invalid Semgrep installation")
    interpreter = directory / "semgrep" / "bin" / "python"
    if (
        not os.access(interpreter, os.X_OK)
        or not interpreter.stat().st_size
        or str(interpreter.resolve()) != manifest.get("python_path")
        or _digest(interpreter.resolve(), MAX_BINARY) != manifest.get("python_sha256")
        or _runtime_digest(metadata[0].parent.parent)
        != manifest.get("semgrep_runtime_sha256")
    ):
        raise ValueError("invalid Semgrep runtime")
    return _component(launcher, SEMGREP_VERSION)


def _typescript_status(directory: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    parser = directory / "typescript" / "typescript.js"
    if manifest.get("typescript_version") != TYPESCRIPT_VERSION or _digest(
        parser, 16 * 1024**2
    ) != manifest.get("typescript_sha256"):
        raise ValueError("invalid TypeScript installation")
    return _component(parser, TYPESCRIPT_VERSION)


def _node_status(directory: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    try:
        node = _node()
    except _SetupError:
        raise ValueError("Node.js is unavailable") from None
    if (
        str(node) != manifest.get("node_path")
        or node.stat().st_size != manifest.get("node_size")
        or node.stat().st_mtime_ns != manifest.get("node_mtime_ns")
    ):
        raise ValueError("Node.js installation changed")
    return {"status": "ready", "path": str(node)}


def _advisory_status(directory: Path) -> dict[str, Any]:
    manifest = _read_json(directory / "manifest.json")
    archive = directory / "osv-scalibr" / "npm" / "all.zip"
    downloaded = manifest.get("downloaded_at")
    modified = manifest.get("source_modified_at")
    if (
        manifest.get("source") != ADVISORY_URL
        or not isinstance(downloaded, (float, int))
        or not isinstance(modified, (float, int))
        or not 0 < modified <= downloaded + 86400
        or not 0 < downloaded <= time.time() + 86400
        or manifest.get("size_bytes") != archive.stat().st_size
        or _digest(archive, MAX_ARCHIVE) != manifest.get("sha256")
    ):
        raise ValueError("invalid advisory snapshot")
    stale = time.time() - min(downloaded, modified) > STALE_SECONDS
    return {
        "status": "stale" if stale else "ready",
        "path": str(directory),
        "source": ADVISORY_URL,
        "downloaded_at": downloaded,
        "source_modified_at": modified,
        "size_bytes": manifest["size_bytes"],
        "sha256": manifest["sha256"],
        "diagnostic": _REMEDIATION
        if stale
        else "Public npm advisory snapshot is available offline.",
    }


def status(directory: Path | None = None) -> dict[str, Any]:
    """Read managed resources without creating directories, launching tools or networking."""
    root = _directory(directory)
    unavailable = {"status": "unavailable", "diagnostic": _REMEDIATION}
    components = {
        key: dict(unavailable)
        for key in ("osv", "semgrep", "typescript", "node", "advisory_db")
    }
    result: dict[str, Any] = {"directory": str(root), "components": components}
    try:
        current = _read_json(root / "current.json")
        if current.get("schema_version") != 1:
            raise ValueError("unknown manifest version")
    except (OSError, ValueError):
        current = {}
    for group in ("tools", "advisories"):
        try:
            generation = _generation(root, group, current.get(group))
            if group == "tools":
                components.update(_tools_status(generation))
            else:
                components["advisory_db"] = _advisory_status(generation)
        except (OSError, ValueError):
            result["diagnostic"] = (
                "Managed resources are absent or invalid. " + _REMEDIATION
            )
    ready = all(item["status"] in {"ready", "stale"} for item in components.values())
    result.update(
        ready=ready,
        status="unavailable"
        if not ready
        else "stale"
        if components["advisory_db"]["status"] == "stale"
        else "ready",
    )
    try:
        last = _read_json(root / "last-setup.json")
        code = last.get("error_code")
        if last.get("status") in {"ready", "error"} and (
            code is None or code in _DIAGNOSTICS
        ):
            result["last_setup"] = {"status": last["status"], "error_code": code}
            if code:
                result["last_setup"]["diagnostic"] = _DIAGNOSTICS[code]
    except (OSError, ValueError, TypeError):
        result["last_setup"] = {"status": "unavailable"}
    return result


def resolve(directory: Path | None = None) -> dict[str, Path]:
    """Return only validated local resources; a stale snapshot remains explicitly usable."""
    return {
        key: Path(item["path"])
        for key, item in status(directory)["components"].items()
        if item["status"] in {"ready", "stale"}
    }


def _size(root: Path) -> int:
    total = 0
    for directory, _, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(directory) / name
            if not path.is_symlink():
                total += path.stat().st_size
    return total


def _storage(root: Path, additional: int = 0) -> None:
    if (
        _size(root) + additional > MAX_STORAGE
        or shutil.disk_usage(root).free < additional
    ):
        raise _SetupError("storage")


@contextmanager
def _lock(root: Path) -> Iterator[None]:
    import fcntl

    with (root / "setup.lock").open("a") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise _SetupError("busy") from None
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _download(url: str, target: Path, limit: int, root: Path) -> dict[str, Any]:
    _storage(root, limit)
    started = time.monotonic()
    total = 0
    digest = hashlib.sha256()
    try:
        with httpx.Client(
            timeout=20, follow_redirects=True, max_redirects=5, trust_env=False
        ) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with target.open("xb") as stream:
                    for chunk in response.iter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > limit or time.monotonic() - started > TIMEOUT:
                            raise _SetupError("download")
                        digest.update(chunk)
                        stream.write(chunk)
                modified = response.headers.get("last-modified", "")
    except httpx.HTTPError:
        raise _SetupError("download") from None
    return {
        "size_bytes": total,
        "sha256": digest.hexdigest(),
        "last_modified": modified,
    }


def _validate_archive(path: Path) -> None:
    started = time.monotonic()
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            if (
                not entries
                or len(entries) > 1_000_000
                or sum(entry.file_size for entry in entries) > MAX_EXPANDED
                or any(
                    entry.file_size > 8 * 1024**2
                    or not re.fullmatch(r"[A-Za-z0-9_.-]+\.json", entry.filename)
                    for entry in entries
                )
            ):
                raise _SetupError("archive")
            for entry in entries:
                if time.monotonic() - started > TIMEOUT:
                    raise _SetupError("archive")
                with archive.open(entry) as stream:
                    while stream.read(1024 * 1024):
                        if time.monotonic() - started > TIMEOUT:
                            raise _SetupError("archive")
    except (BadZipFile, NotImplementedError, RuntimeError, zlib.error):
        raise _SetupError("archive") from None


def _install_command(argv: list[str], directory: Path, root: Path) -> None:
    # No ambient UV/PIP/PYTHON settings, user indexes, project config, credentials,
    # build backends or inherited virtual environment. Installer output is discarded.
    environment = {
        "PATH": os.defpath,
        "HOME": str(directory),
        "TMPDIR": str(directory),
        "UV_HTTP_TIMEOUT": "30",
        "UV_HTTP_RETRIES": "1",
        "UV_CONCURRENT_DOWNLOADS": "4",
    }
    started = time.monotonic()
    with subprocess.Popen(
        argv,
        cwd=directory,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ) as process:
        try:
            while process.poll() is None:
                if time.monotonic() - started > TIMEOUT:
                    raise _SetupError("install")
                _storage(root)
                time.sleep(0.2)
            if process.returncode:
                raise _SetupError("install")
            _storage(root)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


def _install_tools(directory: Path, root: Path) -> None:
    uv = shutil.which(
        "uv",
        path=f"/opt/homebrew/bin:/usr/local/bin:{Path.home() / '.local' / 'bin'}:{os.defpath}",
    )
    if uv is None:
        raise _SetupError("uv")
    node = _node()
    platform_id = _platform()
    url = f"https://github.com/google/osv-scanner/releases/download/v{OSV_VERSION}/osv-scanner_{platform_id}"
    binary = directory / "osv-scanner"
    logger.info("Security setup: downloading the pinned OSV scanner.")
    metadata = _download(url, binary, MAX_BINARY, root)
    if metadata["sha256"] != OSV_DIGESTS[platform_id]:
        raise _SetupError("checksum")
    binary.chmod(0o700)
    environment = directory / "semgrep"
    common = [uv, "--no-config", "--no-cache", "--no-python-downloads"]
    logger.info("Security setup: creating the isolated scanner environment.")
    _install_command(
        [*common, "venv", "--no-project", "--python", sys.executable, str(environment)],
        directory,
        root,
    )
    logger.info("Security setup: installing pinned Semgrep from PyPI wheels.")
    _install_command(
        [
            *common,
            "pip",
            "install",
            "--python",
            str(environment / "bin" / "python"),
            "--default-index",
            "https://pypi.org/simple",
            "--only-binary",
            ":all:",
            "--keyring-provider",
            "disabled",
            f"semgrep=={SEMGREP_VERSION}",
        ],
        directory,
        root,
    )
    parser = _install_typescript(directory, root)
    package_paths = list((environment / "lib").glob("python*/site-packages"))
    if len(package_paths) != 1:
        raise _SetupError("validation")
    _write_json(
        directory / "manifest.json",
        {
            "platform": platform_id,
            "osv_version": OSV_VERSION,
            "semgrep_version": SEMGREP_VERSION,
            "semgrep_launcher_sha256": _digest(
                environment / "bin" / "semgrep", 1024 * 1024
            ),
            "semgrep_runtime_sha256": _runtime_digest(package_paths[0]),
            "python_path": str((environment / "bin" / "python").resolve()),
            "python_sha256": _digest(
                (environment / "bin" / "python").resolve(), MAX_BINARY
            ),
            "typescript_version": TYPESCRIPT_VERSION,
            "typescript_sha256": _digest(parser, 16 * 1024**2),
            "node_path": str(node),
            "node_size": node.stat().st_size,
            "node_mtime_ns": node.stat().st_mtime_ns,
        },
    )
    if any(
        component["status"] != "ready"
        for component in _tools_status(directory).values()
    ):
        raise _SetupError("validation")


def _install_typescript(directory: Path, root: Path) -> Path:
    logger.info(
        "Security setup: downloading and verifying the pinned TypeScript parser."
    )
    archive = directory / "typescript.tgz"
    _download(TYPESCRIPT_URL, archive, 16 * 1024**2, root)
    if (
        base64.b64encode(hashlib.sha512(archive.read_bytes()).digest()).decode()
        != TYPESCRIPT_SHA512
    ):
        raise _SetupError("typescript")
    destination = directory / "typescript"
    destination.mkdir()
    try:
        with tarfile.open(archive, "r:gz") as package:
            for member_name, name in (
                ("package/lib/typescript.js", "typescript.js"),
                ("package/LICENSE.txt", "LICENSE.txt"),
            ):
                member = package.getmember(member_name)
                if not member.isfile() or not 0 < member.size < 16 * 1024**2:
                    raise _SetupError("typescript")
                stream = package.extractfile(member)
                if stream is None:
                    raise _SetupError("typescript")
                with stream, (destination / name).open("xb") as target:
                    shutil.copyfileobj(stream, target)
    except (tarfile.TarError, KeyError):
        raise _SetupError("typescript") from None
    archive.unlink()
    return destination / "typescript.js"


def _install_advisories(directory: Path, root: Path) -> None:
    archive = directory / "osv-scalibr" / "npm" / "all.zip"
    archive.parent.mkdir(parents=True)
    logger.info("Security setup: downloading the public npm advisory snapshot.")
    metadata = _download(ADVISORY_URL, archive, MAX_ARCHIVE, root)
    logger.info("Security setup: validating the public npm advisory snapshot.")
    _validate_archive(archive)
    try:
        modified = parsedate_to_datetime(metadata.pop("last_modified")).timestamp()
        if not 0 < modified <= time.time() + 86400:
            raise ValueError("invalid snapshot timestamp")
    except (ValueError, TypeError, OverflowError):
        raise _SetupError("metadata") from None
    _write_json(
        directory / "manifest.json",
        {
            **metadata,
            "source": ADVISORY_URL,
            "downloaded_at": datetime.now(timezone.utc).timestamp(),
            "source_modified_at": modified,
        },
    )


def _provision(root: Path, refresh: bool) -> None:
    _cleanup_pending(root)
    previous = status(root)
    if previous["ready"] and not refresh:
        logger.info(
            "Security setup: managed resources are already available; use --refresh to update advisories."
        )
        return
    _storage(root)
    created: list[Path] = []
    selected: dict[str, Any] = {"schema_version": 1}
    try:
        components = previous["components"]
        if all(
            components[key]["status"] == "ready"
            for key in ("osv", "semgrep", "typescript", "node")
        ):
            selected["tools"] = Path(components["osv"]["path"]).parent.name
            logger.info("Security setup: reusing the validated pinned tools.")
        else:
            tools = root / "tools" / uuid4().hex
            tools.mkdir(parents=True)
            created.append(tools)
            (tools / ".pending").touch()
            _install_tools(tools, root)
            selected["tools"] = tools.name
        database = root / "advisories" / uuid4().hex
        database.mkdir(parents=True)
        created.append(database)
        (database / ".pending").touch()
        _install_advisories(database, root)
        selected["advisories"] = database.name
        _advisory_status(database)
        _write_json(root / "current.json", selected)
        committed = list(created)
        created.clear()
        for directory in committed:
            (directory / ".pending").unlink()
        logger.info(
            "Security setup: the validated resources are ready for offline checks."
        )
    finally:
        # Publication can complete before _write_json returns (or before bookkeeping
        # below it runs). Persisted selection, never the in-memory created list, is
        # the authority for which generations readers may already be using.
        current: dict[str, Any] | None
        try:
            current = _read_json(root / "current.json")
        except FileNotFoundError:
            current = {}
        except (OSError, ValueError):
            current = None
        for directory in created:
            if (
                current is not None
                and current.get(directory.parent.name) != directory.name
            ):
                shutil.rmtree(directory)


def _cleanup_pending(root: Path) -> None:
    """Under the setup lock, discard only known unfinished, unselected work."""
    try:
        current = _read_json(root / "current.json")
    except FileNotFoundError:
        current = {}
    except ValueError:
        # An unreadable pointer cannot prove which marked generation was committed.
        # Retain it; provisioning can repair selection without deleting reader state.
        return
    for group in ("tools", "advisories"):
        parent = root / group
        if not parent.exists():
            continue
        if parent.is_symlink():
            raise _SetupError("filesystem")
        for generation in parent.iterdir():
            if (
                not re.fullmatch(r"[0-9a-f]{32}", generation.name)
                or generation.is_symlink()
            ):
                continue
            marker = generation / ".pending"
            if not marker.is_file() or marker.is_symlink():
                continue
            if current.get(group) == generation.name:
                marker.unlink()
            else:
                shutil.rmtree(generation)


def setup(directory: Path | None = None, *, refresh: bool = False) -> dict[str, Any]:
    """Download public tools/advisories explicitly, retaining the last good selection."""
    root = _directory(directory)
    error: str | None = None
    try:
        if _platform() not in OSV_DIGESTS:
            raise _SetupError("platform")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Resolve macOS /var and /tmp aliases once; manifests contain no arbitrary paths.
        root = root.resolve()
        with _lock(root):
            try:
                _provision(root, refresh)
            except _SetupError as exc:
                error = str(exc)
            except OSError:
                error = "filesystem"
            except ValueError:
                error = "validation"
            _write_json(
                root / "last-setup.json",
                {"status": "error" if error else "ready", "error_code": error},
            )
    except _SetupError as exc:
        error = str(exc)
    except OSError:
        error = "filesystem"
    result = status(root)
    if error:
        result.update(status="error", error_code=error, diagnostic=_DIAGNOSTICS[error])
    return result
