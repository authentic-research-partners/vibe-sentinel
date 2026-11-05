"""Optional local Semgrep CE source check with a fixed, first-party rule set.

Only a bounded snapshot of JS/TS source reaches the tool. A configured trusted
TypeScript parser encodes bare JSX-text ampersands for Semgrep's parser while
preserving executable bytes and original lines. Project rules,
ignore files, credentials and ambient Semgrep settings never reach it. These
intraprocedural rules identify review candidates, not proof of exploitability.
CLI flags: https://docs.semgrep.dev/cli-reference
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unicodedata
from pathlib import Path
from collections.abc import Iterator
from typing import Any, BinaryIO

from .probes import EXCLUDED_DIRS

RULES = Path(__file__).with_name("security-rules.yml")
JSX_HELPER = Path(__file__).with_name("security-jsx.cjs")
EXTENSIONS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 32_000_000
MAX_FILES = 1000
MAX_ENTRIES = 20_000
MAX_OUTPUT_BYTES = 8_000_000
MESSAGES = {
    "browser-url-to-html": "Browser URL data reaches an HTML interpretation sink.",
    "node-request-to-shell": "Request data reaches a Node shell execution command.",
    "node-request-to-file": "Request data reaches a Node file read path; verify path confinement.",
}


def _result(status: str, summary: str, **fields: Any) -> dict[str, Any]:
    return {
        "id": "js-source",
        "status": status,
        "summary": summary,
        "findings": [],
        **fields,
    }


def _source_paths(base: Path) -> Iterator[Path]:
    def unreadable(exc: OSError) -> None:
        raise exc

    entries = 0
    for directory, dirs, files in base.walk(on_error=unreadable):
        entries += len(dirs) + len(files)
        if entries > MAX_ENTRIES:
            raise ValueError("source traversal limit")
        dirs[:] = sorted(name for name in dirs if name not in EXCLUDED_DIRS)
        for name in sorted(files):
            if name in EXCLUDED_DIRS:
                continue
            path = directory / name
            # Path.walk lists symlinked directories as files by default.
            if path.is_symlink():
                raise ValueError("linked source")
            if path.suffix in EXTENSIONS and path.is_file():
                yield path


def _snapshot(root: Path, source: str, target: Path) -> dict[str, str]:
    """Copy regular in-root source under generated names; reject incomplete input."""
    base = (root / source).resolve()
    if not base.is_relative_to(root) or not base.is_dir():
        raise ValueError("invalid source")
    mapping: dict[str, str] = {}
    total = 0
    for path in _source_paths(base):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("linked source")
        relative = path.relative_to(root).as_posix()
        if len(relative) > 4096 or any(
            unicodedata.category(char).startswith("C") for char in relative
        ):
            raise ValueError("unsafe source path")
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        total += len(data)
        if (
            len(data) > MAX_FILE_BYTES
            or total > MAX_TOTAL_BYTES
            or len(mapping) >= MAX_FILES
        ):
            raise ValueError("source limit")
        name = f"source-{len(mapping)}{path.suffix}"
        (target / name).write_bytes(data)
        mapping[name] = relative
    return mapping


def _parse(payload: Any, target: Path, mapping: dict[str, str]) -> dict[str, Any]:
    """Accept only known rule IDs and locations in the snapshot, never tool prose."""
    if not isinstance(payload, dict):
        raise ValueError("invalid output")
    results = payload.get("results")
    errors = payload.get("errors")
    paths = payload.get("paths")
    if (
        not isinstance(results, list)
        or not isinstance(errors, list)
        or not isinstance(paths, dict)
    ):
        raise ValueError("invalid output")
    scanned = paths.get("scanned")
    if not isinstance(scanned, list) or not all(isinstance(p, str) for p in scanned):
        raise ValueError("invalid paths")

    def original(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("invalid path")
        path = Path(value)
        resolved = (
            (target / path).resolve() if not path.is_absolute() else path.resolve()
        )
        if resolved.parent != target or resolved.name not in mapping:
            raise ValueError("unexpected path")
        return mapping[resolved.name]

    measured = {original(path) for path in scanned}
    findings = []
    seen = set()
    for entry in results:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("check_id"), str)
            or entry["check_id"] not in MESSAGES
        ):
            raise ValueError("invalid finding")
        rule = entry["check_id"]
        path = original(entry.get("path"))
        start = entry.get("start")
        line = start.get("line") if isinstance(start, dict) else None
        if type(line) is not int or line < 1 or line > MAX_FILE_BYTES:
            raise ValueError("invalid line")
        key = (rule, path, line)
        if key not in seen:
            seen.add(key)
            findings.append(
                {
                    "id": rule
                    + ":"
                    + hashlib.sha256(f"{path}:{line}".encode()).hexdigest()[:20],
                    "rule": rule,
                    "message": MESSAGES[rule],
                    "path": path,
                    "line": line,
                }
            )
    # Semgrep's PartialParsing variant carries structured locations. Only that
    # known shape is printable; all other diagnostics remain opaque failures.
    parse_locations: set[tuple[str, int]] = set()
    for error in errors:
        if not isinstance(error, dict) or type(error.get("code")) is not int:
            continue
        kind = error.get("type")
        if (
            error["code"] != 3
            or not isinstance(kind, list)
            or len(kind) != 2
            or kind[0] != "PartialParsing"
            or not isinstance(kind[1], list)
            or not kind[1]
        ):
            continue
        accepted: set[tuple[str, int]] = set()
        for location in kind[1]:
            if not isinstance(location, dict):
                break
            start = location.get("start")
            line = start.get("line") if isinstance(start, dict) else None
            if type(line) is not int or not 1 <= line <= MAX_FILE_BYTES:
                break
            try:
                path = original(location.get("path"))
            except (OSError, ValueError):
                break
            accepted.add((path, line))
        else:
            parse_locations.update(accepted)
    parse_files = len({path for path, _ in parse_locations})
    partial = bool(errors) or measured != set(mapping.values())
    status = "error" if partial else "findings" if findings else "complete"
    return _result(
        status,
        (
            f"Local source check incomplete; partial parsing affects {parse_files} "
            f"{'file' if parse_files == 1 else 'files'}. "
            "Review reported locations and resolve all tool errors, then rerun."
            if parse_files
            else "Local source check incomplete; resolve tool errors and rerun."
        )
        if partial
        else "Local JS/TS source rules completed; review candidates require verification."
        if findings
        else "Local JS/TS source rules completed with no candidates.",
        findings=sorted(findings, key=lambda f: (f["path"], f["line"], f["id"])),
        coverage={
            "files": len(mapping),
            "scanned": len(measured),
            "partial": partial,
            "partial_parse_files": parse_files,
            "partial_parse_locations": [
                {"path": path, "line": line} for path, line in sorted(parse_locations)
            ],
        },
    )


def _invoke(
    argv: list[str],
    target: Path,
    env: dict[str, str],
    output: BinaryIO,
    input_file: BinaryIO | None = None,
) -> int:
    """Bound runtime/output and terminate the whole tool process group on failure."""
    process = subprocess.Popen(
        argv,
        cwd=target,
        env=env,
        stdin=input_file if input_file is not None else subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name == "posix",
    )
    deadline = time.monotonic() + 90
    try:
        while True:
            if (
                time.monotonic() >= deadline
                or os.fstat(output.fileno()).st_size > MAX_OUTPUT_BYTES
            ):
                raise ValueError("tool limit")
            try:
                return process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                # A bounded wait lets us enforce the output limit while it runs.
                continue
    finally:
        if process.poll() is None:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait()


def _jsx_input(
    target: Path,
    mapping: dict[str, str],
    env: dict[str, str],
    node: Path,
    typescript: Path,
) -> int:
    """Encode JSX text through a trusted AST, preserving executable bytes/lines.

    The helper proposes byte offsets, never replacement source or commands.
    Only a literal ampersand may change, to its equivalent character reference.
    Every other byte, including existing entities and embedded expressions, stays.
    """
    with tempfile.TemporaryFile() as input_file, tempfile.TemporaryFile() as output:
        input_file.write(json.dumps(list(mapping)).encode())
        input_file.seek(0)
        code = _invoke(
            [str(node), "--max-old-space-size=512", str(JSX_HELPER), str(typescript)],
            target,
            env,
            output,
            input_file,
        )
        output.seek(0)
        data = output.read(MAX_OUTPUT_BYTES + 1)
    if code != 0 or len(data) > MAX_OUTPUT_BYTES:
        raise ValueError("JSX parser failed")
    edits = json.loads(data)
    if not isinstance(edits, dict) or set(edits) != set(mapping):
        raise ValueError("invalid JSX parser output")
    total = 0
    encodings = 0
    for name, offsets in edits.items():
        original = (target / name).read_bytes()
        if not isinstance(offsets, list):
            raise ValueError("invalid JSX edits")
        previous = -1
        chunks: list[bytes] = []
        for offset in offsets:
            if (
                type(offset) is not int
                or offset <= previous
                or offset >= len(original)
                or original[offset : offset + 1] != b"&"
            ):
                raise ValueError("invalid JSX edit")
            chunks.extend((original[previous + 1 : offset], b"&#38;"))
            previous = offset
        chunks.append(original[previous + 1 :])
        encoded = b"".join(chunks)
        total += len(encoded)
        if len(encoded) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("encoded source limit")
        (target / name).write_bytes(encoded)
        encodings += len(offsets)
    return encodings


def check(
    root: Path,
    source: str,
    *,
    executable: Path | None = None,
    typescript: Path | None = None,
    node: Path | None = None,
) -> dict[str, Any]:
    """Run installed Semgrep CE without fetching rules, installing or executing source."""
    semgrep = str(executable) if executable is not None else shutil.which("semgrep")
    if semgrep is None:
        return _result(
            "unavailable",
            "Semgrep CE is not installed; install it separately to run this check.",
        )
    try:
        root = root.resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="sentinel-source-") as directory:
            target = Path(directory).resolve()
            mapping = _snapshot(root, source, target)
            if not mapping:
                return _result(
                    "unavailable",
                    "No JS/TS source files in scope.",
                    coverage={"files": 0},
                )
            argv = [
                semgrep,
                "scan",
                "--config",
                str(RULES),
                "--oss-only",
                "--metrics=off",
                "--disable-version-check",
                "--disable-nosem",
                "--no-rewrite-rule-ids",
                "--no-git-ignore",
                "--json",
                "--jobs=1",
                "--timeout=5",
                "--max-memory=512",
                f"--max-target-bytes={MAX_FILE_BYTES}",
                ".",
            ]
            # Allowlist instead of stripping known credentials/options. HOME and
            # cwd are private temporary directories, so user/project config is absent.
            env = {
                "PATH": os.environ.get("PATH", os.defpath),
                "HOME": str(target),
                "SEMGREP_SEND_METRICS": "off",
                "SEMGREP_ENABLE_VERSION_CHECK": "0",
                "SEMGREP_SETTINGS_FILE": str(target / "settings.yml"),
            }
            jsx_encodings: int | None = None
            if typescript is not None or node is not None:
                if typescript is None or node is None:
                    return _result(
                        "error",
                        "Trusted JSX parser setup is incomplete; rerun security setup.",
                    )
                try:
                    jsx_encodings = _jsx_input(target, mapping, env, node, typescript)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    return _result(
                        "error",
                        "Trusted JSX input parsing failed; check source syntax and security setup, then rerun.",
                    )
            with tempfile.TemporaryFile() as output:
                returncode = _invoke(argv, target, env, output)
                output.seek(0)
                data = output.read(MAX_OUTPUT_BYTES + 1)
            if returncode != 0 or len(data) > MAX_OUTPUT_BYTES:
                return _result(
                    "error",
                    "Semgrep CE failed or exceeded output limits; check the installation and rerun.",
                )
            result = _parse(json.loads(data), target, mapping)
            if jsx_encodings is not None:
                result["coverage"].update(
                    jsx_input_adapter="typescript-jsx-text-entities",
                    jsx_text_encodings=jsx_encodings,
                )
            return result
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return _result(
            "error",
            "Local source check incomplete; check source access, limits and Semgrep CE installation, then rerun.",
        )
