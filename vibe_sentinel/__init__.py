"""vibe-sentinel: a guard on how a codebase is organized.

Not a linter. It does not read your code for defects — it measures the
*shape* of the codebase, records that shape, and tells you when it moves:
directories that were meant to stay small growing, code appearing where
it never lived before, organizational patterns showing up in new layers.

The probes are scripts you point at your own company's expectations,
with their parameters declared in your config. Measuring is mechanical. A
local LLM — a different model family from whatever wrote the code — then
reads the resulting changes to say which ones matter, and judges the
credential candidates the rules flagged. It never chooses what to
measure. Any OpenAI-compatible backend works: vLLM, Ollama, llama.cpp,
LM Studio.

A scan answers two questions, and they are kept apart on purpose. *What
moved* is drift, diffed against a baseline you accept deliberately.
*What is true* comes from the gates — licences, dependency provenance,
credentials at rest — and is reported on every run, because a key in the
tree is as true on the two-hundredth scan as the first. Only a pin
settles one of those; a new baseline never does.

Beside both, and neither: *where it is going*. One baseline shows drift
at one scale, so a scan also compares against the newest run a week and
a month old, and fits every recorded series — a Theil–Sen slope with a
Mann–Kendall test, and each new value scored against a fit made without
it. Two modules a week clears no tolerance and is a reorganisation by
Christmas. These are readings: they move no baseline, are never stored,
and reach no exit code.

Python API::

    import asyncio
    from pathlib import Path
    from vibe_sentinel import load_config, load_probes, run_gates, scan_and_compare

    async def main():
        config = load_config()
        snapshot, drift, run_id = await scan_and_compare(
            load_probes(), Path("."), config
        )
        state = await run_gates(Path("."), config, run_id=run_id)
        return drift, state

    drift, state = asyncio.run(main())
    print(len(drift.changes), "moved;", len(state.failing), "standing")

**Every import here is lazy, deliberately.** ``vibe-sentinel hook`` runs
as a separate process in front of every tool call the coding agent makes,
so the cost of importing this package is paid thousands of times in a
session. Eagerly re-exporting the scan pipeline meant pulling in httpx
and the whole model client to record one shell command. Names resolve on
first attribute access (PEP 562), and the ``TYPE_CHECKING`` block keeps
them real types for mypy and for editors.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.2.0"

if TYPE_CHECKING:
    from vibe_sentinel.analyze import analyze_drift
    from vibe_sentinel.backend import start as backend_start
    from vibe_sentinel.backend import status as backend_status
    from vibe_sentinel.config import SentinelConfig, load_config
    from vibe_sentinel.db import SchemaMismatchError, get_db, get_status, run_migration
    from vibe_sentinel.exceptions import LLMConnectionError, VibeSentinelError
    from vibe_sentinel.engine import run_gates, scan, scan_and_compare
    from vibe_sentinel.inventory import compare
    from vibe_sentinel.report import render_agent, render_json, render_terminal
    from vibe_sentinel.journal import (
        AgentSessionRecord,
        CommandRecord,
        HookEvent,
    )
    from vibe_sentinel.schemas import (
        Change,
        DriftReport,
        GateFinding,
        GateReport,
        GateState,
        Observation,
        ProbeResult,
        RunRecord,
        Snapshot,
        TrendPoint,
    )
    from vibe_sentinel.templates import Placeholder, Probe, load_probes, select_probes


#: Exported name -> (module, attribute). Two names are aliased, which is
#: why this maps to a pair rather than just a module.
_LAZY: dict[str, tuple[str, str]] = {
    "AgentSessionRecord": ("vibe_sentinel.journal", "AgentSessionRecord"),
    "Change": ("vibe_sentinel.schemas", "Change"),
    "CommandRecord": ("vibe_sentinel.journal", "CommandRecord"),
    "DriftReport": ("vibe_sentinel.schemas", "DriftReport"),
    "GateFinding": ("vibe_sentinel.schemas", "GateFinding"),
    "GateReport": ("vibe_sentinel.schemas", "GateReport"),
    "GateState": ("vibe_sentinel.schemas", "GateState"),
    "HookEvent": ("vibe_sentinel.journal", "HookEvent"),
    "HorizonDrift": ("vibe_sentinel.schemas", "HorizonDrift"),
    "LLMConnectionError": ("vibe_sentinel.exceptions", "LLMConnectionError"),
    "Observation": ("vibe_sentinel.schemas", "Observation"),
    "Placeholder": ("vibe_sentinel.templates", "Placeholder"),
    "Probe": ("vibe_sentinel.templates", "Probe"),
    "ProbeResult": ("vibe_sentinel.schemas", "ProbeResult"),
    "RunRecord": ("vibe_sentinel.schemas", "RunRecord"),
    "SchemaMismatchError": ("vibe_sentinel.db", "SchemaMismatchError"),
    "SentinelConfig": ("vibe_sentinel.config", "SentinelConfig"),
    "Snapshot": ("vibe_sentinel.schemas", "Snapshot"),
    "TrendFit": ("vibe_sentinel.schemas", "TrendFit"),
    "TrendPoint": ("vibe_sentinel.schemas", "TrendPoint"),
    "VibeSentinelError": ("vibe_sentinel.exceptions", "VibeSentinelError"),
    "analyze_drift": ("vibe_sentinel.analyze", "analyze_drift"),
    "backend_start": ("vibe_sentinel.backend", "start"),
    "backend_status": ("vibe_sentinel.backend", "status"),
    "compare": ("vibe_sentinel.inventory", "compare"),
    "fit_series": ("vibe_sentinel.trends", "fit_series"),
    "get_db": ("vibe_sentinel.db", "get_db"),
    "get_status": ("vibe_sentinel.db", "get_status"),
    "load_config": ("vibe_sentinel.config", "load_config"),
    "load_probes": ("vibe_sentinel.templates", "load_probes"),
    "render_agent": ("vibe_sentinel.report", "render_agent"),
    "render_json": ("vibe_sentinel.report", "render_json"),
    "render_terminal": ("vibe_sentinel.report", "render_terminal"),
    "run_gates": ("vibe_sentinel.engine", "run_gates"),
    "run_migration": ("vibe_sentinel.db", "run_migration"),
    "scan": ("vibe_sentinel.engine", "scan"),
    "scan_and_compare": ("vibe_sentinel.engine", "scan_and_compare"),
    "select_probes": ("vibe_sentinel.templates", "select_probes"),
}


def __getattr__(name: str) -> Any:
    """Resolve an exported name on first use (PEP 562).

    The result is cached in ``globals()``, so the import cost is paid at
    most once per name per process and never at all for a command that
    does not touch it.
    """
    try:
        module_name, attribute = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(importlib.import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "AgentSessionRecord",
    "Change",
    "CommandRecord",
    "DriftReport",
    "GateFinding",
    "GateReport",
    "GateState",
    "HookEvent",
    "HorizonDrift",
    "LLMConnectionError",
    "Observation",
    "Placeholder",
    "Probe",
    "ProbeResult",
    "RunRecord",
    "SchemaMismatchError",
    "SentinelConfig",
    "Snapshot",
    "TrendFit",
    "TrendPoint",
    "VibeSentinelError",
    "__version__",
    "analyze_drift",
    "backend_start",
    "backend_status",
    "compare",
    "fit_series",
    "get_db",
    "get_status",
    "load_config",
    "load_probes",
    "render_agent",
    "render_json",
    "render_terminal",
    "run_migration",
    "run_gates",
    "scan",
    "scan_and_compare",
    "select_probes",
]
