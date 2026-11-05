"""Stdlib-only inventory helper, runnable by an isolated Python 3.10+ target.

Never put inspected directories on sys.path or execute .pth lines. The protocol
contains metadata, not application imports, and errors never quote that metadata.
"""

from __future__ import annotations

import csv
import email.parser
import importlib.machinery
import json
import os
import platform
import re
import site
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

MAX_FILE_BYTES = 512 * 1024
MAX_LICENSE_BYTES = 1024 * 1024
MAX_DISTRIBUTIONS = 10000
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MARKER_KEYS = frozenset(
    {
        "implementation_name",
        "implementation_version",
        "os_name",
        "platform_machine",
        "platform_release",
        "platform_system",
        "platform_version",
        "python_full_version",
        "platform_python_implementation",
        "python_version",
        "sys_platform",
    }
)


def read_bounded(path: Path, limit: int = MAX_FILE_BYTES) -> str:
    """Read a bounded text file; absent optional metadata is a distinct empty value."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
    except FileNotFoundError:
        return ""
    if len(raw) > limit:
        raise ValueError("metadata file exceeds inspection limit")
    return raw.decode("utf-8")


def clean_direct_url(raw: str) -> str:
    """Keep provenance, dropping URL credentials, queries and fragments."""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        url = urlsplit(data.get("url", ""))
        host = url.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if url.port is not None:
            host += f":{url.port}"
        result: dict[str, Any] = {
            "url": urlunsplit((url.scheme, host, url.path, "", ""))
        }
        vcs = data.get("vcs_info")
        if isinstance(vcs, dict):
            result["vcs_info"] = {
                key: str(vcs[key]) for key in ("vcs", "commit_id") if key in vcs
            }
        directory = data.get("dir_info")
        if isinstance(directory, dict):
            result["dir_info"] = {"editable": bool(directory.get("editable"))}
        return json.dumps(result)
    except (TypeError, ValueError, AttributeError):
        raise ValueError("invalid direct URL metadata") from None


def clean_requirement(raw: str) -> str:
    """Remove direct URLs before metadata crosses the subprocess protocol."""
    if not re.match(r"^\s*[A-Za-z0-9][A-Za-z0-9._-]*(?:\s*\[[^\]]*\])?\s*@", raw):
        return raw
    head, _, tail = raw.partition("@")
    # PEP 508 separates a URL from a following marker with whitespace.
    marker = re.search(r"\s+;\s*(.+)$", tail)
    return head.strip() + (f"; {marker.group(1)}" if marker else "")


def marker_environment() -> dict[str, str]:
    version = sys.implementation.version
    implementation = f"{version.major}.{version.minor}.{version.micro}"
    if version.releaselevel != "final":
        implementation += version.releaselevel[0] + str(version.serial)
    return {
        "implementation_name": sys.implementation.name,
        "implementation_version": implementation,
        "os_name": os.name,
        "platform_machine": platform.machine(),
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "platform_version": platform.version(),
        "python_full_version": platform.python_version(),
        "platform_python_implementation": platform.python_implementation(),
        "python_version": ".".join(platform.python_version_tuple()[:2]),
        "sys_platform": sys.platform,
    }


def identity(prefix: Path) -> dict[str, str]:
    cfg = read_bounded(prefix / "pyvenv.cfg")
    if (prefix / "conda-meta").is_dir():
        kind = "conda"
    elif cfg or str(prefix) != sys.base_prefix:
        kind = (
            "poetry"
            if "pypoetry" in str(prefix)
            else "uv"
            if re.search(r"^uv\s*=", cfg, re.MULTILINE)
            else "venv"
        )
    else:
        kind = "system"
    return {
        "kind": kind,
        "name": prefix.name,
        "prefix": str(prefix),
        "python": platform.python_version(),
        "executable": sys.executable,
    }


def _normal_user_site_allowed() -> bool:
    """site.check_enableusersite's normal policy without our forced -I flag."""
    setting = os.environ.get("PYTHONNOUSERSITE", "")
    try:
        disabled = int(setting) != 0 if setting else False
    except ValueError:
        disabled = True
    if disabled:
        return False
    if hasattr(os, "getuid") and hasattr(os, "geteuid") and os.getuid() != os.geteuid():
        return False
    if hasattr(os, "getgid") and hasattr(os, "getegid") and os.getgid() != os.getegid():
        return False
    return True


def target_paths() -> tuple[Path, list[Path], list[str]]:
    """Account for pre-3.14 -S losing the venv prefix without running site.main."""
    executable = Path(sys.executable).absolute()
    candidates = (executable.parent, executable.parent.parent)
    prefix = next(
        (path for path in candidates if (path / "pyvenv.cfg").is_file()),
        Path(sys.prefix),
    )
    config_path = prefix / "pyvenv.cfg"
    is_venv = config_path.is_file()
    config = {
        key.strip().lower(): value.strip().lower()
        for line in read_bounded(config_path).splitlines()
        if "=" in line
        for key, _, value in [line.partition("=")]
    }
    # site.venv defaults this key to true when a config exists but omits it.
    system_site = (
        not is_venv or config.get("include-system-site-packages", "true") == "true"
    )
    user_site = system_site and _normal_user_site_allowed()
    roots = (
        [Path(path) for path in site.getsitepackages([str(prefix)])] if is_venv else []
    )
    if user_site:
        user = site.getusersitepackages()
        roots.extend(Path(path) for path in ([user] if isinstance(user, str) else user))
    if system_site:
        prefixes = (
            [sys.base_prefix, sys.base_exec_prefix]
            if is_venv
            else [sys.prefix, sys.exec_prefix]
        )
        roots.extend(Path(path) for path in site.getsitepackages(prefixes))
    paths: list[Path] = []
    limitations: list[str] = []
    # site.main orders venv, user, then system; each site's .pth paths follow it.
    # Added .pth directories do not themselves process further .pth files.
    for directory in dict.fromkeys(path for path in roots if path.is_dir()):
        if directory not in paths:
            paths.append(directory)
        for pth in sorted(directory.glob("*.pth")):
            for line in read_bounded(pth).splitlines():
                text = line.strip()
                if not text or text.startswith("#"):
                    continue
                if text.startswith(("import ", "import\t")):
                    limitations.append(
                        "Executable .pth hooks were not run; dynamic import paths are unverified."
                    )
                    continue
                path = directory / text
                if path.is_dir() and path not in paths:
                    paths.append(path)
                elif path.exists() and not path.is_dir():
                    raise ValueError("archive import paths are unsupported")
    for directory in [
        *paths,
        *(Path(path) for path in sys.path if path and Path(path).is_dir()),
    ]:
        for name in (
            ("sitecustomize", "usercustomize") if user_site else ("sitecustomize",)
        ):
            if (directory / name).is_dir() or any(
                (directory / f"{name}{suffix}").is_file()
                for suffix in (".py", ".pyc", *importlib.machinery.EXTENSION_SUFFIXES)
            ):
                limitations.append(
                    "Startup customization modules were not run; dynamic import paths are unverified."
                )
    return prefix, paths, sorted(set(limitations))


def license_files(metadata_path: Path) -> list[list[str]]:
    """Capture only files beneath the distribution's own metadata directory."""
    if not metadata_path.is_dir():
        return []
    root = metadata_path.resolve()
    records = read_bounded(metadata_path / "RECORD")
    candidates: set[Path] = set()
    for row in csv.reader(records.splitlines()):
        if not row:
            continue
        path = metadata_path.parent / row[0]
        if Path(row[0]).name.upper().startswith(("LICENSE", "COPYING")):
            # Package-data licence files belong to another resolver scope. Only
            # a path claiming this distribution's metadata directory is read.
            if not Path(row[0]).parts or Path(row[0]).parts[0] != metadata_path.name:
                continue
            if not path.resolve().is_relative_to(root):
                raise ValueError("license file escapes distribution metadata directory")
            candidates.add(path)
    # Legacy metadata can lack RECORD. Inspect its own licence directory only.
    for directory in (metadata_path, metadata_path / "licenses"):
        if directory.is_dir():
            for path in directory.iterdir():
                if (
                    path.name.upper().startswith(("LICENSE", "COPYING"))
                    and path.is_file()
                ):
                    if not path.resolve().is_relative_to(root):
                        raise ValueError(
                            "license file escapes distribution metadata directory"
                        )
                    candidates.add(path)
    out: list[list[str]] = []
    for path in sorted(candidates):
        if not path.is_file():
            raise ValueError("recorded license file is missing")
        out.append([path.name, read_bounded(path, MAX_LICENSE_BYTES)])
    return out


def capture_metadata(metadata_path: Path) -> dict[str, Any]:
    text = (
        read_bounded(metadata_path / "METADATA")
        if metadata_path.is_dir()
        else read_bounded(metadata_path)
    )
    if not text and metadata_path.is_dir():
        text = read_bounded(metadata_path / "PKG-INFO")
    meta = email.parser.Parser().parsestr(text)
    installer = (
        read_bounded(metadata_path / "INSTALLER").strip()
        if metadata_path.is_dir()
        else ""
    )
    direct = (
        read_bounded(metadata_path / "direct_url.json")
        if metadata_path.is_dir()
        else ""
    )
    wheel = read_bounded(metadata_path / "WHEEL") if metadata_path.is_dir() else ""
    return {
        "name": meta.get("Name", ""),
        "version": meta.get("Version", ""),
        "requires": [
            clean_requirement(raw) for raw in meta.get_all("Requires-Dist", [])
        ],
        "installer": installer,
        "direct_url": clean_direct_url(direct),
        "provenance_recorded": bool(installer or direct or wheel),
        "summary": meta.get("Summary", "")[:200],
        "license_expression": meta.get("License-Expression", ""),
        "license": meta.get("License", ""),
        "classifiers": meta.get_all("Classifier", []),
        "license_files": license_files(metadata_path),
        "top_level": read_bounded(metadata_path / "top_level.txt").split()
        if metadata_path.is_dir()
        else [],
        "record": read_bounded(metadata_path / "RECORD")
        if metadata_path.is_dir()
        else "",
    }


def top_level_name(filename: str) -> str:
    name = filename.split("/", 1)[0]
    for suffix in (*importlib.machinery.EXTENSION_SUFFIXES, ".py", ".pyc"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name if name.isidentifier() else ""


def collect_target() -> dict[str, Any]:
    if sys.version_info < (3, 10):
        raise ValueError("target requires Python 3.10 or newer")
    prefix, paths, limitations = target_paths()
    distributions: list[dict[str, Any]] = []
    owners: dict[str, list[str]] = {}
    importable: set[str] = set()
    captured_bytes = 0
    for directory in paths:
        for child in sorted(directory.iterdir()):
            module = (
                top_level_name(child.name)
                if child.is_dir()
                or child.name.endswith(
                    (*importlib.machinery.EXTENSION_SUFFIXES, ".py", ".pyc")
                )
                else ""
            )
            if module:
                importable.add(module)
            if child.name.endswith((".dist-info", ".egg-info")):
                dist = capture_metadata(child)
                if not dist["name"]:
                    raise ValueError("installed metadata has no distribution name")
                modules = dist.pop("top_level")
                record = dist.pop("record")
                if not modules:
                    modules = [
                        top_level_name(row[0])
                        for row in csv.reader(record.splitlines())
                        if row
                    ]
                for module in set(modules):
                    if module and module.isidentifier():
                        owners.setdefault(module, []).append(dist["name"])
                captured_bytes += len(json.dumps(dist, ensure_ascii=True).encode())
                if captured_bytes > MAX_OUTPUT_BYTES:
                    raise ValueError("inventory exceeds inspection limit")
                distributions.append(dist)
                if len(distributions) > MAX_DISTRIBUTIONS:
                    raise ValueError("distribution count exceeds inspection limit")
            elif child.name.endswith((".egg", ".zip")):
                raise ValueError("archive installations are unsupported")
    return {
        "environment": identity(prefix),
        "distributions": distributions,
        "marker_environment": marker_environment(),
        "stdlib_modules": sorted(sys.stdlib_module_names),
        "import_owners": owners,
        "importable": sorted(importable),
        "limitations": limitations,
    }


def main() -> int:
    try:
        payload = json.dumps(collect_target(), ensure_ascii=True)
        if len(payload.encode()) > MAX_OUTPUT_BYTES:
            raise ValueError("inventory exceeds inspection limit")
        sys.stdout.write(payload)
        return 0
    except (OSError, ValueError, UnicodeError):
        # Metadata and arbitrary child diagnostics never reach the caller's UI.
        sys.stdout.write(
            json.dumps(
                {
                    "error": "Target metadata could not be inspected safely; check Python >=3.10, metadata size, and supported directory installations."
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
