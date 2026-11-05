"""Measure declared Vite artifact weights without reading or running output code.

Generated chunk names are not stable identities. Output files are grouped by
manifest slot roles and logical entry reachability, preserving shared ownership
without guessing which anonymous chunk a previous build emitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
from typing import Any

from vibe_sentinel.relationships import edge_observation

MAX_BYTES = 8 * 1024 * 1024
MAX_CHUNKS = 10_000
MAX_FILES = 20_000
MAX_LINKS = 50_000
MAX_WORK = 200_000
MAX_OWNER_BYTES = 8 * 1024 * 1024
MAX_OBSERVATIONS = 30_000
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_TEXT = 2048
FIELDS = frozenset(
    {
        "file",
        "src",
        "name",
        "isEntry",
        "isDynamicEntry",
        "imports",
        "dynamicImports",
        "css",
        "assets",
    }
)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise ValueError("invalid JSON number")


def text(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= MAX_TEXT
        and not any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value)
    )


def relative(value: Any) -> bool:
    return (
        text(value)
        and not any(char in value for char in "\\:?#")
        and not value.startswith("/")
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


class Reader:
    """A bounded manifest projection; completeness refers only to this artifact."""

    def __init__(self, root: Path, manifest: str, out_dir: str) -> None:
        self.root = root.resolve()
        self.manifest = manifest
        self.out_dir = out_dir
        self.chunks: dict[str, dict[str, Any]] = {}
        self.files: dict[str, dict[str, Any]] = {}
        self.observations: dict[str, dict[str, Any]] = {}
        self.gaps: set[str] = set()
        self.work = 0
        self.owner_bytes = 0
        self.output_bytes = 0

    def gap(self, code: str) -> None:
        # Never interpolate an unvalidated path or arbitrary manifest metadata.
        self.gaps.add(code)

    def path(self, value: str, *, directory: bool = False) -> Path:
        if not relative(value):
            raise ValueError("invalid relative path")
        current = self.root
        parts = PurePosixPath(value).parts
        for index, part in enumerate(parts):
            current /= part
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ValueError("symlink")
            if index < len(parts) - 1 or directory:
                if not stat.S_ISDIR(mode):
                    raise ValueError("not directory")
            elif not stat.S_ISREG(mode):
                raise ValueError("not regular file")
        return current

    def load(self) -> bool:
        try:
            self.path(self.out_dir, directory=True)
            artifact = self.path(self.manifest)
            artifact.relative_to(self.root / self.out_dir)
            flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(artifact, flags), "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("not regular file")
                data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                self.gap("manifest-size-limit")
                return False
            document = json.loads(
                data.decode("utf-8"),
                object_pairs_hook=unique_object,
                parse_constant=reject_constant,
            )
            if not isinstance(document, dict):
                raise ValueError("manifest must be an object")
            if len(document) > MAX_CHUNKS:
                self.gap("manifest-chunk-limit")
                return False
            links = 0
            for key, chunk in document.items():
                if not text(key) or not isinstance(chunk, dict):
                    raise ValueError("invalid chunk")
                if chunk.keys() - FIELDS:
                    self.gap("unsupported-manifest-field")
                if not relative(chunk.get("file")):
                    raise ValueError("invalid output path")
                for field in ("src", "name"):
                    if field in chunk and not text(chunk[field]):
                        raise ValueError("invalid name")
                for field in ("isEntry", "isDynamicEntry"):
                    if field in chunk and not isinstance(chunk[field], bool):
                        raise ValueError("invalid entry flag")
                if chunk.get("isEntry") or chunk.get("isDynamicEntry"):
                    if not relative(key):
                        raise ValueError("invalid logical entry")
                for field in ("imports", "dynamicImports", "css", "assets"):
                    values = chunk.get(field, [])
                    if not isinstance(values, list) or not all(text(v) for v in values):
                        raise ValueError("invalid relationship list")
                    if field in {"css", "assets"} and not all(
                        relative(v) for v in values
                    ):
                        raise ValueError("invalid asset path")
                    links += len(values)
                if links > MAX_LINKS:
                    self.gap("manifest-link-limit")
                    return False
                self.chunks[key] = chunk
            for chunk in self.chunks.values():
                for target in chunk.get("imports", []) + chunk.get(
                    "dynamicImports", []
                ):
                    if target not in self.chunks:
                        self.gap("missing-chunk-reference")
            return not self.gaps
        except (OSError, ValueError, UnicodeError, RecursionError):
            self.gap("unreadable-or-invalid-manifest")
            return False

    def record(
        self, kind: str, identity: Any, metric: str, value: int, **attrs: Any
    ) -> None:
        key = "vite:1:" + digest([kind, identity, metric])
        self.add(
            {
                "key": key,
                "value": value,
                "state": canonical(attrs)
                if kind == "entry" and metric == "present"
                else "",
                "label": f"{kind} {metric}",
                "attrs": {
                    "kind": kind,
                    "identity": canonical(identity),
                    "metric": metric,
                    **attrs,
                },
            }
        )

    def add(self, observation: dict[str, Any]) -> None:
        if len(self.observations) >= MAX_OBSERVATIONS:
            self.gap("observation-limit")
            return
        size = len(canonical(observation).encode())
        if self.output_bytes + size > MAX_OUTPUT_BYTES:
            self.gap("output-limit")
            return
        self.output_bytes += size
        self.observations[observation["key"]] = observation

    def budget(self) -> bool:
        self.work += 1
        if self.work > MAX_WORK:
            self.gap("graph-work-limit")
            return False
        return True

    def gather(self) -> bool:
        for chunk in self.chunks.values():
            slots = [(chunk["file"], "output")]
            slots += [(name, "css") for name in chunk.get("css", [])]
            slots += [(name, "asset") for name in chunk.get("assets", [])]
            for name, role in slots:
                if name not in self.files:
                    if len(self.files) >= MAX_FILES:
                        self.gap("artifact-file-limit")
                        return False
                    try:
                        file = self.path(f"{self.out_dir}/{name}")
                        size = file.lstat().st_size
                        self.files[name] = {
                            "bytes": size,
                            "roles": set(),
                            "owners": set(),
                        }
                    except (OSError, ValueError):
                        self.gap("missing-or-unsafe-output-file")
                        return False
                self.files[name]["roles"].add(role)
        return True

    def closure(self, entry: str) -> tuple[set[str], set[str]] | None:
        pending = [(entry, False)]
        visited: set[tuple[str, bool]] = set()
        static_files: set[str] = set()
        dynamic_files: set[str] = set()
        while pending:
            key, dynamic = pending.pop()
            if not self.budget():
                return None
            if (key, dynamic) in visited:
                continue
            visited.add((key, dynamic))
            chunk = self.chunks[key]
            files = dynamic_files if dynamic else static_files
            for name in [
                chunk["file"],
                *chunk.get("css", []),
                *chunk.get("assets", []),
            ]:
                if not self.budget():
                    return None
                files.add(name)
            pending.extend((target, dynamic) for target in chunk.get("imports", []))
            pending.extend((target, True) for target in chunk.get("dynamicImports", []))
        return static_files, dynamic_files - static_files

    def project(self) -> None:
        entries = sorted(
            key
            for key, chunk in self.chunks.items()
            if chunk.get("isEntry") or chunk.get("isDynamicEntry")
        )
        if not entries:
            self.gap("no-logical-entries")
            return
        if not self.gather():
            return
        entry_set = set(entries)
        for entry in entries:
            chunk = self.chunks[entry]
            for field, phase in (("imports", "static"), ("dynamicImports", "dynamic")):
                for target in sorted(set(chunk.get(field, [])) & entry_set):
                    self.add(
                        edge_observation(
                            "vite",
                            f"entry:{entry}",
                            f"entry:{target}",
                            "import",
                            phase,
                            "manifest",
                        )
                    )
            reached = self.closure(entry)
            if reached is None:
                return
            chunk = self.chunks[entry]
            self.record(
                "entry",
                entry,
                "present",
                1,
                is_entry=str(bool(chunk.get("isEntry"))).lower(),
                is_dynamic_entry=str(bool(chunk.get("isDynamicEntry"))).lower(),
            )
            for phase, names in zip(("static", "dynamic"), reached, strict=True):
                self.record(
                    "entry",
                    entry,
                    f"{phase}-raw-bytes",
                    sum(self.files[name]["bytes"] for name in names),
                    unit="bytes",
                )
                self.record("entry", entry, f"{phase}-files", len(names), unit="files")
                for name in names:
                    if not self.budget():
                        return
                    self.owner_bytes += len(entry.encode())
                    if self.owner_bytes > MAX_OWNER_BYTES:
                        self.gap("owner-identity-limit")
                        return
                    self.files[name]["owners"].add((entry, phase))
        cohorts: dict[str, dict[str, Any]] = {}
        for file in self.files.values():
            roles = sorted(file["roles"])
            owners = sorted(file["owners"])
            identity = [roles, owners]
            key = digest(identity)
            cohort = cohorts.setdefault(
                key, {"identity": identity, "bytes": 0, "files": 0}
            )
            cohort["bytes"] += file["bytes"]
            cohort["files"] += 1
        for key, cohort in sorted(cohorts.items()):
            identity = cohort["identity"]
            for metric in ("bytes", "files"):
                self.record(
                    "cohort",
                    identity,
                    f"raw-{metric}" if metric == "bytes" else metric,
                    cohort[metric],
                    unit=metric,
                )
            for entry, phase in identity[1]:
                self.add(
                    edge_observation(
                        "vite",
                        f"entry:{entry}",
                        f"cohort:{key}",
                        "artifact-reachability",
                        phase,
                        "manifest",
                    )
                )
        self.record(
            "build",
            "manifest-declared",
            "raw-bytes",
            sum(file["bytes"] for file in self.files.values()),
            unit="bytes",
        )
        self.record(
            "build", "manifest-declared", "files", len(self.files), unit="files"
        )

    def measure(self) -> dict[str, Any]:
        if self.load():
            self.project()
        scope = {
            "definition": "vite-manifest-projection-v1",
            "manifest": self.manifest if relative(self.manifest) else "invalid",
            "out_dir": self.out_dir if relative(self.out_dir) else "invalid",
            "freshness": "not-assessed",
            "runtime_performance": "not-assessed",
            "weights": "raw-file-bytes",
            "compression": "not-measured",
            "attribution": "unique-path-globally-full-weight-per-entry",
            "coverage": "manifest-declared-files-only",
        }
        self.add(
            {
                "key": "vite:1:scope",
                "state": canonical(scope),
                "label": "Vite artifact scope",
                "attrs": {"kind": "scope"},
            }
        )
        result = {
            "observations": [
                self.observations[key] for key in sorted(self.observations)
            ],
            "summary": "Vite manifest artifact projection; source freshness not assessed",
            "coverage": {
                "status": "incomplete" if self.gaps else "complete",
                "gaps": [
                    {"code": code, "scope": "artifact"} for code in sorted(self.gaps)
                ],
            },
        }
        if len(canonical(result).encode()) > MAX_OUTPUT_BYTES:
            result["observations"] = []
            result["coverage"] = {
                "status": "incomplete",
                "gaps": [{"code": "output-limit", "scope": "artifact"}],
            }
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure logical Vite build entries and raw artifact weights without running a build."
    )
    parser.add_argument(
        "--root", required=True, help="Project root containing the existing artifacts"
    )
    parser.add_argument(
        "--manifest",
        required=True,
        help="Existing Vite manifest JSON path relative to root",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        help="Existing build output directory relative to root",
    )
    args = parser.parse_args(argv)
    result = Reader(Path(args.root), args.manifest, args.out_dir).measure()
    print(canonical(result))
    if result["coverage"]["status"] != "complete":
        sample = canonical(result["coverage"]["gaps"][:2])
        print(
            f"Vite artifact coverage incomplete: {sample}. Supply a supported existing manifest and regular output files, then rerun this probe.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
