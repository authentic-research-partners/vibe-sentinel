"""Small shared I/O and evidence boundaries for declaration artifact readers."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
from typing import Any

from vibe_sentinel.relationships import edge_observation

MAX_BYTES = 8 * 1024 * 1024
MAX_NODES = 100_000
MAX_DEPTH = 128
MAX_TEXT = 2048
MAX_OBSERVATIONS = 30_000
MAX_OUTPUT_BYTES = 16 * 1024 * 1024


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def text(value: Any, *, empty: bool = False) -> bool:
    return (
        isinstance(value, str)
        and (empty or bool(value.strip()))
        and len(value) <= MAX_TEXT
        and not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
    )


def pointer(parent: str, name: str | int) -> str:
    return parent + "/" + str(name).replace("~", "~0").replace("/", "~1")


def relative(value: str) -> bool:
    return (
        text(value)
        and not value.startswith("/")
        and "\\" not in value
        and ":" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def contained(root: Path, relative_path: str, *, directory: bool = False) -> Path:
    if not relative(relative_path):
        raise ValueError("invalid relative path")
    current = root
    parts = relative_path.split("/")
    for index, part in enumerate(parts):
        current /= part
        mode = current.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError("symlink")
        expected = stat.S_ISDIR if index < len(parts) - 1 or directory else stat.S_ISREG
        if not expected(mode):
            raise ValueError("unexpected file type")
    return current


def read_bytes(root: Path, path: str, limit: int = MAX_BYTES) -> bytes:
    file = contained(root, path)
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(file, flags), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("not regular")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("input byte limit")
    return data


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite number")
    return number


def reject_constant(value: str) -> None:
    raise ValueError("nonfinite number")


def json_document(data: bytes) -> Any:
    value = json.loads(
        data.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_float=finite_float,
        parse_constant=reject_constant,
    )
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError("document complexity limit")
        if isinstance(item, dict):
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
    return value


class Evidence:
    """Bounded observations and structural diagnostic scopes, never raw data."""

    def __init__(self, ecosystem: str) -> None:
        self.ecosystem = ecosystem
        self.observations: dict[str, dict[str, Any]] = {}
        self.gaps: set[tuple[str, str]] = set()
        self.output_bytes = 0

    def gap(self, code: str, scope: str = "artifact") -> None:
        if len(self.gaps) < 100:
            self.gaps.add((code, scope if text(scope) else "artifact"))

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

    def record(self, kind: str, identity: str, state: Any, **attrs: str) -> None:
        if not text(identity):
            self.gap("structural-identity-limit")
            return
        key = (
            f"{self.ecosystem}:1:"
            + hashlib.sha256(canonical([kind, identity]).encode()).hexdigest()
        )
        self.add(
            {
                "key": key,
                "value": 1,
                "state": canonical(state),
                "label": f"{kind} {identity}",
                "attrs": {"kind": kind, "identity": identity, **attrs},
            }
        )

    def edge(self, source: str, target: str, kind: str) -> None:
        if not text(source) or not text(target):
            self.gap("structural-identity-limit")
            return
        self.add(
            edge_observation(
                self.ecosystem, source, target, kind, "declaration", "local"
            )
        )

    def result(self, summary: str) -> dict[str, Any]:
        result = {
            "observations": [
                self.observations[key] for key in sorted(self.observations)
            ],
            "summary": summary,
            "coverage": {
                "status": "incomplete" if self.gaps else "complete",
                "gaps": [
                    {"code": code, "scope": scope} for code, scope in sorted(self.gaps)
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


def emit(result: dict[str, Any], subject: str) -> int:
    print(canonical(result))
    if result["coverage"]["status"] != "complete":
        codes = ", ".join(gap["code"] for gap in result["coverage"]["gaps"][:2])
        print(
            f"{subject} coverage incomplete: {codes}. Supply supported declarations inside the selected root; rerun this reader for full JSON coverage.",
            file=sys.stderr,
        )
        return 2
    return 0
