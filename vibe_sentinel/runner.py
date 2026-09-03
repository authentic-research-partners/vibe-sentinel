"""Run a probe and parse what it printed.

The probe's parameters come from its ``[[probe.placeholders]]`` tables and
nothing else. The model is not asked, and used to be: it saw the
repository layout and chose the values.

The measurement that settled it, from this project's own history: across
nineteen scans of a repository whose layout never changed, the model
answered ``SOURCE_ROOT`` as ``"."`` on eight of them, ``"vibe_sentinel"``
on ten, and ``"vibe_sentinel/"`` on one — four changes of mind about the
same directory. Each flip re-keys every observation, so the next
comparison reports a codebase that was reorganised overnight. That is a
whole class of drift manufactured by the instrument.

It bought nothing in exchange. Every shipped placeholder has a default
that is right in any repository (``"."``, ``"*.py"``, ``"python"``), so
the call could only agree with a constant or deviate from it. Where a
project genuinely needs a different value, it writes one in its config
once — which is also the only way the answer is the same twice.

What survives from that arrangement is the part that was load-bearing:
:meth:`Probe.fill` validates every value against the placeholder's
declared ``pattern`` before it reaches an argv, substitution is
per-element, and ``shell=False``. That boundary is about what can reach a
command line, not about where the value came from, so it holds unchanged
now that the value comes from a config file.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from loguru import logger
from pydantic import ValidationError

from vibe_sentinel.schemas import Observation, ProbeResult
from vibe_sentinel.templates import Probe


#: Every parse failure names the protocol, because the fix is always a
#: change to what the probe prints.
_PROTOCOL = (
    "A probe must print {'observations': [...], 'summary': '...'} to stdout, "
    "and diagnostics to stderr."
)


def run_probe(probe: Probe, values: dict[str, str], cwd: Path) -> ProbeResult:
    """Execute one filled probe and parse its output.

    A probe that fails is recorded as a failed result rather than raising:
    one broken template must not lose the other probes' measurements, and
    a silently absent probe would look like "nothing changed" on the next
    drift comparison.
    """
    result = ProbeResult(probe_id=probe.id, filled=values)
    try:
        argv = probe.fill(values)
    except ValueError as e:
        result.ok = False
        result.error = str(e)
        logger.error("probe {}: {}", probe.id, e)
        return result

    result.command = argv
    logger.info("probe {}: running {}", probe.id, " ".join(argv))
    try:
        completed = subprocess.run(  # noqa: S603  # fixed argv, shell=False
            argv,
            capture_output=True,
            text=True,
            timeout=probe.timeout_s,
            cwd=cwd,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        result.ok = False
        result.error = f"probe did not run: {e}"
        logger.error("probe {}: {}", probe.id, result.error)
        return result

    if completed.returncode != 0:
        result.ok = False
        result.error = f"exit {completed.returncode}: {completed.stderr.strip()[:500] or '(no stderr)'}"
        logger.error("probe {}: {}", probe.id, result.error)
        return result

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as e:
        result.ok = False
        result.error = (
            f"probe printed output that is not the JSON protocol: {e}. {_PROTOCOL}"
        )
        logger.error("probe {}: {}", probe.id, result.error)
        return result

    # Valid JSON is not yet the protocol, and each of these used to raise
    # straight through engine.scan's loop — losing every other probe's
    # measurements because one probe printed the wrong shape.
    if not isinstance(payload, dict):
        result.ok = False
        result.error = (
            f"probe printed {type(payload).__name__}, not the JSON object the "
            f"protocol expects. {_PROTOCOL}"
        )
        logger.error("probe {}: {}", probe.id, result.error)
        return result

    raw = payload.get("observations", [])
    if not isinstance(raw, list):
        result.ok = False
        result.error = (
            f"probe printed 'observations' as {type(raw).__name__}, not a "
            f"list. {_PROTOCOL}"
        )
        logger.error("probe {}: {}", probe.id, result.error)
        return result

    try:
        result.observations = [Observation.model_validate(o) for o in raw]
    except ValidationError as e:
        result.ok = False
        result.error = (
            f"probe printed an observation the protocol does not accept: {e}. "
            f"Every observation needs a 'key' that is stable across runs; "
            f"'value' is a number, or omitted where only existence is measured."
        )
        logger.error("probe {}: {}", probe.id, result.error)
        return result

    result.summary = str(payload.get("summary", ""))
    logger.info("probe {}: {} observation(s)", probe.id, len(result.observations))
    return result
