"""Offline aggregate Markdown from a captured versioned scan result.

Input is data, including any strings a project or model wrote. Only fixed
status vocabulary, numbers and sanitized provenance reach the summary.
"""

from __future__ import annotations

from collections import Counter
import json
import math
import os
from pathlib import Path
import stat
from typing import Any

from vibe_sentinel.results import parse_result, project_result
from vibe_sentinel.schemas import VersionedScanResult

MAX_BYTES = 16 * 1024 * 1024
MAX_NODES = 500_000
MAX_DEPTH = 64


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _finite(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Nonfinite JSON number")
    return number


def _constant(value: str) -> None:
    raise ValueError("Nonfinite JSON number")


def read_result(path: Path) -> VersionedScanResult:
    """Read one regular, bounded UTF-8 export, without loading project state."""
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Not a regular file")
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("Input limit")
        payload = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique,
            parse_float=_finite,
            parse_constant=_constant,
        )
        pending = [(payload, 0)]
        count = 0
        while pending:
            item, depth = pending.pop()
            count += 1
            if count > MAX_NODES or depth > MAX_DEPTH:
                raise ValueError("Input complexity limit")
            if isinstance(item, dict):
                pending.extend((value, depth + 1) for value in item.values())
            elif isinstance(item, list):
                pending.extend((value, depth + 1) for value in item)
        if isinstance(payload, dict) and set(payload) == {"result"}:
            payload = payload["result"]
        if not isinstance(payload, dict):
            raise ValueError("Result object required")
        for field in ("schema_version", "exit_code"):
            if type(payload.get(field)) is not int:
                raise ValueError("Integer result field required")
        for field in ("run_id", "baseline_run_id"):
            value = payload.get(field)
            if value is not None and type(value) is not int:
                raise ValueError("Integer or null run identity required")
        counts = payload.get("counts")
        if not isinstance(counts, dict) or any(
            type(value) is not int for value in counts.values()
        ):
            raise ValueError("Integer count required")
        return parse_result(payload)
    except (OSError, ValueError, TypeError, RecursionError):
        raise ValueError(
            "Cannot summarize this input: provide a supported, complete JSON result envelope in a regular UTF-8 file within the documented limits."
        ) from None


def render_summary(result: VersionedScanResult) -> str:
    """Summarize the strict aggregate projection, even for local-detail input."""
    result = parse_result(result.model_dump(mode="json"))
    safe = project_result(result, profile="shareable")["result"]
    provenance = safe["provenance"]
    counts = safe["counts"]
    revision = provenance["source_revision"] or "unavailable"
    dirty = {True: "yes", False: "no", None: "unknown"}[provenance["source_dirty"]]
    comparison = {
        "compared": "Compared with the recorded baseline",
        "no_baseline": "No baseline comparison was available",
        "incomplete": "Baseline comparison is incomplete",
        "not_run": "Baseline comparison did not run",
    }[safe["comparison"]]
    lines = [
        "## Vibe Sentinel recorded scan",
        "",
        f"Outcome: **{safe['outcome']}**. Recorded scan exit code: **{safe['exit_code']}**.",
        "",
        f"- Captured revision: `{revision}`",
        f"- Source kind: `{provenance['source_kind']}`; dirty at capture: **{dirty}**",
        f"- Recorded at: `{safe['generated_at'] or 'unavailable'}`",
        f"- Run: {safe['run_id'] if safe['run_id'] is not None else 'unavailable'}; baseline run: {safe['baseline_run_id'] if safe['baseline_run_id'] is not None else 'unavailable'}",
        f"- Comparison: {comparison}.",
        "- Source capture is not atomic; a revision does not identify uncommitted changes.",
        "- Build freshness: **not assessed**. Judge weights and tool build identity: **unverified**.",
        "",
        "| Recorded measurement | Count |",
        "| --- | ---: |",
    ]
    for label, key in (
        ("Probes", "probes"),
        ("Observations", "observations"),
        ("Baseline changes", "changes"),
        ("State findings", "findings"),
        ("Failing state findings", "failing_findings"),
        ("Probes missing comparable baseline coverage", "baseline_unmeasured"),
    ):
        lines.append(f"| {label} | {counts[key]} |")
    lines.extend(["", "| Stage | Recorded statuses |", "| --- | --- |"])
    for label, field in (
        ("Probes", "probes"),
        ("State gates", "gates"),
        ("Judges", "judges"),
        ("Longer horizons", "horizons"),
    ):
        statuses = Counter(stage["status"] for stage in safe[field])
        description = "; ".join(
            f"{status}: {count}" for status, count in sorted(statuses.items())
        )
        lines.append(f"| {label} | {description or 'No stage records'} |")
    lines.extend(["", f"Gate collection: `{safe['gates_status']}`."])
    if safe["failed_stages"]:
        lines.append("Failed stages: " + ", ".join(safe["failed_stages"]) + ".")
    if safe.get("evidence", {}).get("failure"):
        lines.append(
            "Detailed evidence capture exceeded its bounds and is unavailable."
        )
    lines.extend(
        [
            "",
            "This aggregate omits finding identities, paths, questions, answers and diagnostics. Counts do not replace coverage or judgement statuses.",
        ]
    )
    return "\n".join(lines) + "\n"
