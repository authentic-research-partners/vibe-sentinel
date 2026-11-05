"""One explicit boundary between the scanner and the Python it measures.

Configured targets run a bundled stdlib-only reader with -I -S. No failure of
that reader substitutes the scanner's installed packages or marker values.
"""

from __future__ import annotations

import email.message
import importlib.metadata as md
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vibe_sentinel import _python_inventory as reader
from vibe_sentinel.config import SentinelConfig, find_config, load_config
from vibe_sentinel.requirements import normalize


class EnvironmentInspectionError(ValueError):
    """The selected environment could not be measured; never a clean inventory."""


def validate_marker_environment(value: Mapping[str, str]) -> dict[str, str]:
    """Prevent packaging from filling absent target values from the scanner."""
    if set(value) != reader.MARKER_KEYS or any(not text for text in value.values()):
        raise EnvironmentInspectionError("Target marker environment is incomplete.")
    return dict(value)


class Environment(BaseModel):
    """Measured interpreter identity and the independent scanner identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: str
    name: str
    prefix: str
    python: str
    executable: str
    selection: Literal["runtime", "configured"] = "runtime"
    requested_executable: str = ""
    scanner_executable: str = Field(default_factory=lambda: sys.executable)

    @field_validator("kind", "name", "prefix", "python", "executable")
    @classmethod
    def required_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("interpreter identity is incomplete")
        return value

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.name}"


class DistributionSnapshot(BaseModel):
    """Captured distribution facts, with no live import or filesystem methods."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    version: str
    requires: tuple[str, ...] = ()
    installer: str = ""
    direct_url: str = ""
    provenance_recorded: bool = False
    summary: str = ""
    license_expression: str = ""
    license: str = ""
    classifiers: tuple[str, ...] = ()
    license_files: tuple[tuple[str, str], ...] = ()

    @property
    def metadata(self) -> email.message.Message:
        meta = email.message.Message()
        for key, value in (
            ("Name", self.name),
            ("Version", self.version),
            ("Summary", self.summary),
            ("License-Expression", self.license_expression),
            ("License", self.license),
        ):
            if value:
                meta[key] = value
        for requirement in self.requires:
            meta["Requires-Dist"] = requirement
        for classifier in self.classifiers:
            meta["Classifier"] = classifier
        return meta


class CapturedDistribution(md.Distribution):
    """The existing licence resolver's interface over already captured facts."""

    def __init__(self, snapshot: DistributionSnapshot) -> None:
        self.snapshot = snapshot
        self.captured_license_files = snapshot.license_files

    @property
    def metadata(self) -> Any:
        # Return the captured headers directly. Re-serializing multiline licence
        # fields through an email generator can reject valid installed metadata.
        return self.snapshot.metadata

    def read_text(self, filename: str) -> str | None:
        if filename in {"METADATA", "PKG-INFO"}:
            return (
                "\n".join(
                    f"{key}: "
                    + str(value)
                    .replace("\r\n", "\n")
                    .replace("\r", "\n")
                    .replace("\n", "\n ")
                    for key, value in self.metadata.items()
                )
                + "\n"
            )
        if filename == "INSTALLER":
            return self.snapshot.installer
        if filename == "direct_url.json":
            return self.snapshot.direct_url
        if filename == "WHEEL" and self.snapshot.provenance_recorded:
            return "Captured install record"
        return None

    def locate_file(self, path: str | os.PathLike[str]) -> Path:
        raise EnvironmentInspectionError(
            "Captured metadata has no live filesystem paths."
        )


class EnvironmentSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    environment: Environment
    distributions: tuple[DistributionSnapshot, ...]
    marker_environment: dict[str, str]
    stdlib_modules: frozenset[str]
    import_owners: dict[str, tuple[str, ...]]
    importable: frozenset[str]
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def complete_target(self) -> EnvironmentSnapshot:
        if self.environment.selection == "configured":
            if not {"sys", "builtins"} <= self.stdlib_modules:
                raise ValueError("target standard library inventory is incomplete")
            if (
                not Path(self.environment.prefix).is_absolute()
                or not Path(self.environment.executable).is_absolute()
            ):
                raise ValueError("target interpreter identity must use absolute paths")
        return self

    @field_validator("marker_environment")
    @classmethod
    def complete_markers(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_marker_environment(value)

    @field_validator("import_owners")
    @classmethod
    def normalized_owners(
        cls, value: dict[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        return {
            module: tuple(sorted({normalize(name) for name in names}))
            for module, names in value.items()
        }

    def license_distributions(self) -> list[md.Distribution]:
        return [CapturedDistribution(dist) for dist in self.distributions]


def runtime_environment() -> Environment:
    """The current process's identity, shared by every in-process consumer."""
    prefix = Path(sys.prefix)
    data = reader.identity(prefix)
    conda = os.environ.get("CONDA_PREFIX")
    if conda and Path(conda) == prefix:
        data.update(
            kind="conda", name=os.environ.get("CONDA_DEFAULT_ENV") or prefix.name
        )
    elif sys.prefix != sys.base_prefix and os.environ.get("POETRY_ACTIVE"):
        data["kind"] = "poetry"
    return Environment(**data)


def _runtime_snapshot() -> EnvironmentSnapshot:
    """Keep the unconfigured, already-running interpreter's path semantics."""
    distributions: list[DistributionSnapshot] = []
    for dist in md.distributions():
        meta = dist.metadata
        if not meta.get("Name"):
            continue
        installer = (dist.read_text("INSTALLER") or "").strip()
        direct = dist.read_text("direct_url.json") or ""
        files: tuple[tuple[str, str], ...] = ()
        metadata_path = getattr(dist, "_path", None)
        if isinstance(metadata_path, Path):
            files = tuple(
                (name, text) for name, text in reader.license_files(metadata_path)
            )
        distributions.append(
            DistributionSnapshot(
                name=str(meta["Name"]),
                version=str(meta.get("Version", "")),
                requires=tuple(
                    reader.clean_requirement(raw)
                    for raw in meta.get_all("Requires-Dist") or []
                ),
                installer=installer,
                direct_url=reader.clean_direct_url(direct),
                provenance_recorded=bool(
                    installer or direct or dist.read_text("WHEEL")
                ),
                summary=str(meta.get("Summary", ""))[:200],
                license_expression=str(meta.get("License-Expression", "")),
                license=str(meta.get("License", "")),
                classifiers=tuple(meta.get_all("Classifier") or []),
                license_files=files,
            )
        )
    owners = {
        name: tuple(normalize(dist) for dist in dists)
        for name, dists in md.packages_distributions().items()
    }
    return EnvironmentSnapshot(
        environment=runtime_environment(),
        distributions=tuple(distributions),
        marker_environment=reader.marker_environment(),
        stdlib_modules=frozenset(sys.stdlib_module_names),
        import_owners=owners,
        importable=frozenset(owners),
    )


def _run_reader(executable: Path, root: Path, timeout: float) -> bytes:
    """Bound the pipe in memory and the child in wall time; discard diagnostics."""
    argv = [str(executable), "-I", "-S", str(Path(reader.__file__).absolute())]
    try:
        process = subprocess.Popen(
            argv,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            start_new_session=os.name == "posix",
        )
    except OSError:
        raise EnvironmentInspectionError(
            "Configured Python could not be started. Fix [environment] python to an existing executable path."
        ) from None
    output = bytearray()
    oversized = threading.Event()
    assert process.stdout is not None
    stream = process.stdout

    def terminate() -> None:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return  # The group already exited; shutdown is complete.
            except PermissionError:
                # macOS can deny a signal while an exiting process has not yet
                # been reaped. Confirm that exit; a still-running child remains
                # an operational error rather than silently skipped cleanup.
                process.wait(timeout=0.1)
                return
        elif process.poll() is None:
            process.kill()

    def drain() -> None:
        while True:
            block = stream.read(min(65536, reader.MAX_OUTPUT_BYTES + 1 - len(output)))
            if not block:
                return
            output.extend(block)
            if len(output) > reader.MAX_OUTPUT_BYTES:
                oversized.set()
                return

    thread = threading.Thread(target=drain, daemon=True)
    deadline = time.monotonic() + timeout
    thread.start()
    try:
        thread.join(timeout)
        if thread.is_alive():
            raise subprocess.TimeoutExpired(argv, timeout)
        if oversized.is_set():
            terminate()
            raise EnvironmentInspectionError(
                "Target inventory exceeds the bounded output limit."
            )
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if process.returncode:
            raise EnvironmentInspectionError(
                "Configured Python inspection failed. Use Python >=3.10 with readable, bounded "
                "directory metadata and licence files confined to their distribution metadata directory."
            )
        return bytes(output)
    except subprocess.TimeoutExpired:
        raise EnvironmentInspectionError(
            "Configured Python inspection timed out. Check the interpreter or adjust [environment] timeout."
        ) from None
    finally:
        if process.poll() is None or thread.is_alive():
            terminate()
        process.wait()
        thread.join(1)
        if not thread.is_alive():
            stream.close()


def collect_environment(
    root: Path, config: SentinelConfig | None = None
) -> EnvironmentSnapshot:
    """Read the selected environment without importing application code."""
    if config is None:
        path = find_config(root)
        config = load_config(path) if path else SentinelConfig(project_dir=root)
    if not config.environment_python:
        try:
            return _runtime_snapshot()
        except (OSError, ValueError, UnicodeError):
            raise EnvironmentInspectionError(
                "Runtime metadata could not be inspected safely."
            ) from None
    requested = Path(config.environment_python)
    executable = requested if requested.is_absolute() else root / requested
    executable = (
        executable.absolute()
    )  # resolve() would discard a venv's symlink identity.
    payload = _run_reader(executable, root, config.environment_timeout)
    try:
        data: dict[str, Any] = json.loads(payload)
        data["environment"].update(
            selection="configured",
            requested_executable=str(executable),
            scanner_executable=sys.executable,
        )
        snapshot = EnvironmentSnapshot.model_validate(data)
        if tuple(int(part) for part in snapshot.environment.python.split(".")[:2]) < (
            3,
            10,
        ):
            raise ValueError("unsupported target")
        return snapshot
    except (ValueError, KeyError, TypeError, AttributeError):
        raise EnvironmentInspectionError(
            "Configured Python returned an invalid or incomplete inventory. Check the target interpreter and metadata."
        ) from None
