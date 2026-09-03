"""Running probes.

No model appears here, and that is the point: a probe's parameters come
from its config and nothing else, so running one reaches the filesystem
and nothing further.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


from vibe_sentinel.runner import run_probe
from vibe_sentinel.templates import Placeholder, Probe


def _echo_probe(payload: dict, **kw) -> Probe:
    """A probe that just prints a fixed protocol payload."""
    base = {
        "id": "p",
        "title": "t",
        "command": [sys.executable, "-c", f"print({json.dumps(json.dumps(payload))})"],
    }
    return Probe(**{**base, **kw})


_PAYLOAD = {
    "observations": [{"key": "a", "value": 2.0, "label": "a is 2"}],
    "summary": "one observation",
}


# --- running ---------------------------------------------------------------


def test_probe_output_is_parsed(tmp_path: Path) -> None:
    result = run_probe(_echo_probe(_PAYLOAD), {}, cwd=tmp_path)
    assert result.ok
    assert result.summary == "one observation"
    assert result.by_key()["a"].value == 2.0


def test_filled_values_are_recorded_on_the_result(tmp_path: Path) -> None:
    """The report shows what the probe was actually pointed at, so a run
    can be understood later without re-deriving the model's choice."""
    probe = _echo_probe(
        _PAYLOAD,
        command=[
            sys.executable,
            "-c",
            "print('{}')".format(json.dumps(_PAYLOAD).replace("'", "")),
        ],
    )
    result = run_probe(probe, {"UNUSED": "x"}, cwd=tmp_path)
    assert result.filled == {"UNUSED": "x"}


def test_nonzero_exit_is_recorded_not_raised(tmp_path: Path) -> None:
    """One broken probe must not lose the other probes' measurements."""
    probe = Probe(
        id="p", title="t", command=[sys.executable, "-c", "import sys; sys.exit(3)"]
    )
    result = run_probe(probe, {}, cwd=tmp_path)
    assert result.ok is False
    assert "exit 3" in result.error


def test_non_json_output_is_a_clear_error(tmp_path: Path) -> None:
    probe = Probe(id="p", title="t", command=[sys.executable, "-c", "print('hello')"])
    result = run_probe(probe, {}, cwd=tmp_path)
    assert result.ok is False
    assert "JSON protocol" in result.error


def test_json_that_is_not_an_object_is_recorded(tmp_path: Path) -> None:
    """Valid JSON is not yet the protocol."""
    probe = Probe(id="p", title="t", command=[sys.executable, "-c", "print('[1, 2]')"])
    result = run_probe(probe, {}, cwd=tmp_path)
    assert result.ok is False
    assert "not the JSON object" in result.error


def test_observations_that_are_not_a_list_are_recorded(tmp_path: Path) -> None:
    result = run_probe(
        _echo_probe({"observations": {"a": 1}, "summary": "s"}), {}, cwd=tmp_path
    )
    assert result.ok is False
    assert "not a list" in result.error


def test_a_schema_invalid_observation_is_recorded_not_raised(tmp_path: Path) -> None:
    """The same rule a non-zero exit obeys: one probe printing the wrong
    shape must not lose the other probes' measurements."""
    result = run_probe(
        _echo_probe({"observations": [{"value": 1.0}], "summary": "s"}),
        {},
        cwd=tmp_path,
    )
    assert result.ok is False
    assert "does not accept" in result.error
    assert result.observations == []


def test_a_missing_binary_is_recorded(tmp_path: Path) -> None:
    probe = Probe(id="p", title="t", command=["definitely-not-a-real-binary-xyz"])
    result = run_probe(probe, {}, cwd=tmp_path)
    assert result.ok is False
    assert "did not run" in result.error


def test_a_rejected_placeholder_stops_the_run(tmp_path: Path) -> None:
    """Validation happens before exec, so a bad value never reaches a
    command line."""
    probe = Probe(
        id="p",
        title="t",
        command=["echo", "{NAME}"],
        placeholders=[Placeholder(name="NAME", description="n")],
    )
    result = run_probe(probe, {"NAME": "a; rm -rf /"}, cwd=tmp_path)
    assert result.ok is False
    assert "does not match" in result.error
    assert result.command == []


def test_timeout_is_recorded(tmp_path: Path) -> None:
    probe = Probe(
        id="p",
        title="t",
        command=[sys.executable, "-c", "import time; time.sleep(5)"],
        timeout_s=0.3,
    )
    result = run_probe(probe, {}, cwd=tmp_path)
    assert result.ok is False
    assert "did not run" in result.error
