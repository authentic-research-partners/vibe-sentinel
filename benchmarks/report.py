"""Rendering a run, and writing it down. Nothing here measures anything.

Three surfaces from one object: a terminal table for whoever is watching,
a markdown file for the record that gets committed, and JSON for whatever
compares two runs later.

The renderer has one rule, and it is the one ``DriftReport.unmeasured``
enforces upstream: a number nobody can act on must not render as one they
can. A pass with unanswered requests says so on its own row, because a
condition that was fast because a third of its requests came back
unparseable is not a fast condition, and the wall clock alone cannot tell
you which you are holding.
"""

from __future__ import annotations

import json
import platform
import statistics
import shutil
import subprocess
from datetime import UTC, datetime
from itertools import chain
from pathlib import Path

from pydantic import BaseModel, Field

from benchmarks.harness import Suite
from vibe_sentinel.config import SentinelConfig
from vibe_sentinel.llm import check_endpoint


class Environment(BaseModel):
    """What a duration here is a duration *of*.

    Deliberately no hostname, no user, no paths: a result file is written
    to be committed and read by strangers, and the machine's identity is
    not part of the measurement. What is: the card, the CPU, the model
    that answered, and the two settings that change what a request costs.
    """

    when: str
    gpu: str = "unknown"
    cpu: str = "unknown"
    python: str = ""
    model: str = ""
    served: str = ""
    structured_output: str = ""
    max_tokens: int = 0
    temperature: float = 0.0
    note: str = ""


class Run(BaseModel):
    """One invocation: an environment, and the suites that ran in it."""

    environment: Environment
    requests: int
    repeat: int
    suites: list[Suite] = Field(default_factory=list)


def _gpu() -> str:
    """The card, per nvidia-smi. 'unknown' where there is no nvidia-smi.

    Not a fallback: nothing downstream substitutes a different card, and a
    run on a machine without one is still a real run whose result file
    should say what it could not find out.
    """
    binary = shutil.which("nvidia-smi")
    if binary is None:
        return "unknown (no nvidia-smi)"
    try:
        out = subprocess.run(
            [binary, "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return f"unknown ({type(e).__name__})"
    lines = [line.strip() for line in out.stdout.splitlines() if line.strip()]
    return "; ".join(lines) if lines else "unknown (nvidia-smi said nothing)"


def _cpu() -> str:
    """The CPU model name, from /proc/cpuinfo where there is one."""
    info = Path("/proc/cpuinfo")
    if info.exists():
        for line in info.read_text(errors="replace").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    return platform.processor() or "unknown"


async def describe(config: SentinelConfig, note: str = "") -> Environment:
    """Gather the provenance, including what the endpoint says it serves."""
    _, served = await check_endpoint(config)
    return Environment(
        when=datetime.now(UTC).strftime("%Y-%m-%d"),
        gpu=_gpu(),
        cpu=_cpu(),
        python=platform.python_version(),
        model=config.llm_model,
        served=served,
        structured_output=config.structured_output,
        max_tokens=config.max_tokens,
        temperature=config.temperature,
        note=note,
    )


def _rows(suite: Suite) -> list[tuple[str, ...]]:
    """One row per measurement, in the order :data:`_HEAD` names."""
    if not suite.measurements:
        return []
    baseline = suite.measurements[0].seconds
    rows = []
    for m in suite.measurements:
        ratio = baseline / m.seconds if m.seconds else 0.0
        notes = []
        if m.unanswered:
            notes.append(f"{m.unanswered} unanswered")
        if m.truncated:
            notes.append(f"{m.truncated} truncated+retried")
        flag = f"  ({', '.join(notes)})" if notes else ""
        rows.append(
            (
                m.label + flag,
                f"{m.seconds:.2f}"
                + ("" if m.spread is None else f" ±{m.spread * 100:.0f}%"),
                f"{m.per_second:.2f}",
                f"{statistics.median(chain.from_iterable(p.latencies for p in m.passes)):.2f}",
                f"{max(p.slowest for p in m.passes):.2f}",
                f"{ratio:.2f}x",
            )
        )
    return rows


_HEAD = ("condition", "seconds", "req/s", "median", "slowest", "vs first")


def _table(suite: Suite, indent: str = "  ") -> list[str]:
    rows = _rows(suite)
    if not rows:
        return [f"{indent}nothing measured"]
    widths = [
        max(len(_HEAD[i]), max(len(row[i]) for row in rows)) for i in range(len(_HEAD))
    ]

    def line(cells: tuple[str, ...]) -> str:
        first = cells[0].ljust(widths[0])
        rest = "  ".join(c.rjust(widths[i + 1]) for i, c in enumerate(cells[1:]))
        return f"{indent}{first}  {rest}"

    return [line(_HEAD), line(tuple("-" * w for w in widths)), *(line(r) for r in rows)]


def render(run: Run) -> str:
    """The terminal rendering."""
    env = run.environment
    lines = [
        "",
        "=" * 74,
        "  vibe-sentinel benchmarks — what this tool's model traffic costs",
        "=" * 74,
        f"  {env.when}   {env.model}  ({env.structured_output}, "
        f"max_tokens={env.max_tokens}, temperature={env.temperature})",
        f"  GPU: {env.gpu}",
        f"  {run.requests} requests per pass, median of {run.repeat} pass(es), "
        f"each preceded by a discarded warm-up",
    ]
    if env.note:
        lines.append(f"  note: {env.note}")
    for suite in run.suites:
        lines += ["", f"  {suite.id}: {suite.question}"]
        if suite.note:
            lines.append(f"    {suite.note}")
        lines.append("")
        lines += _table(suite, indent="    ")
    lines.append("")
    return "\n".join(lines)


def markdown(run: Run) -> str:
    """The committed record. Same numbers, in a table a reader can diff."""
    env = run.environment
    lines = [
        f"# Benchmark run — {env.when}",
        "",
        "What this tool's model traffic costs on one machine. Not a gate and",
        "not a threshold: read it as the evidence behind the `concurrency`",
        "defaults, and re-run it on your own backend before trusting either.",
        "",
        "## Environment",
        "",
        "| | |",
        "| --- | --- |",
        f"| date | {env.when} |",
        f"| model | `{env.model}` |",
        f"| endpoint reports | {env.served} |",
        f"| structured output | `{env.structured_output}` |",
        f"| max_tokens / temperature | {env.max_tokens} / {env.temperature} |",
        f"| GPU | {env.gpu} |",
        f"| CPU | {env.cpu} |",
        f"| Python | {env.python} |",
        f"| requests per pass | {run.requests} |",
        f"| passes (median reported) | {run.repeat}, after a discarded warm-up |",
    ]
    if env.note:
        lines.append(f"| note | {env.note} |")
    for suite in run.suites:
        lines += ["", f"## {suite.id}", "", suite.question, ""]
        if suite.note:
            lines += [suite.note, ""]
        rows = _rows(suite)
        lines.append("| " + " | ".join(_HEAD) + " |")
        lines.append("| " + " | ".join("---" for _ in _HEAD) + " |")
        for row in rows:
            lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    return "\n".join(lines)


def to_json(run: Run) -> str:
    return json.dumps(run.model_dump(mode="json"), indent=2, sort_keys=True)


def write(run: Run, directory: Path) -> list[Path]:
    """Write the markdown and the JSON. Returns what it wrote."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    suites = "-".join(s.id for s in run.suites)
    base = directory / f"{stamp}-{suites}"
    md, js = base.with_suffix(".md"), base.with_suffix(".json")
    md.write_text(markdown(run), encoding="utf-8")
    js.write_text(to_json(run), encoding="utf-8")
    return [md, js]
