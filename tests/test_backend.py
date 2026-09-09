"""Starting, stopping and checking the model backend.

The module is deliberately thin — it runs the command in your config and
probes the endpoint over HTTP — so what is worth pinning is the thinness.
No shell is involved, nothing is inferred about containers or GPUs, and
every failure names what to do about it.

Nothing here starts a real backend: the endpoint check is mocked and the
commands are ``true`` / ``false``.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from vibe_sentinel import backend as backend_mod
from vibe_sentinel.backend import _resolve, stop
from vibe_sentinel.config import SentinelConfig
from vibe_sentinel.schemas import BackendMetrics

# `status` and `start` are async: nothing in this package opens an event
# loop except the command that runs it, so a test is the loop here. The
# shims keep every assertion below about the backend rather than about
# asyncio.


def status(config: SentinelConfig) -> int:
    return asyncio.run(backend_mod.status(config))


def start(config: SentinelConfig, wait: bool = True) -> int:
    return asyncio.run(backend_mod.start(config, wait=wait))


@pytest.fixture
def unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def answer(_config: SentinelConfig) -> tuple[bool, str]:
        return False, "ConnectError: nothing listening"

    monkeypatch.setattr(backend_mod, "check_endpoint", answer)


@pytest.fixture
def reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def answer(_config: SentinelConfig) -> tuple[bool, str]:
        return True, "serving: qwen3"

    monkeypatch.setattr(backend_mod, "check_endpoint", answer)


# --- status ----------------------------------------------------------------


def test_status_reports_what_is_served(
    reachable: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert status(SentinelConfig()) == 0
    out = capsys.readouterr().out
    assert "ready" in out
    assert "qwen3" in out


def test_status_on_an_unreachable_endpoint_exits_one(
    unreachable: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert status(SentinelConfig()) == 1
    assert "unreachable" in capsys.readouterr().out


def test_status_says_how_to_start_it_when_it_knows(
    unreachable: None, capsys: pytest.CaptureFixture[str]
) -> None:
    config = SentinelConfig(start_command=["ollama", "serve"])
    status(config)
    assert "vibe-sentinel backend start" in capsys.readouterr().out


def test_status_says_it_cannot_start_it_when_it_does_not_know(
    unreachable: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Empty means "I start it myself", which is a supported answer and
    has to read like one rather than like a missing feature."""
    status(SentinelConfig())
    assert "No [llm] start_command configured" in capsys.readouterr().out


# --- start and stop --------------------------------------------------------


def test_start_without_a_command_is_a_setup_error(unreachable: None) -> None:
    """2, not 1: nothing was checked and nothing failed the check."""
    assert start(SentinelConfig(), wait=False) == 2


def test_stop_without_a_command_is_a_setup_error() -> None:
    assert stop(SentinelConfig()) == 2


def test_start_does_nothing_when_it_is_already_running(
    reachable: None, capsys: pytest.CaptureFixture[str]
) -> None:
    config = SentinelConfig(start_command=["false"])
    assert start(config, wait=False) == 0
    assert "Already running" in capsys.readouterr().out


def test_start_runs_the_configured_command(unreachable: None) -> None:
    assert start(SentinelConfig(start_command=["true"]), wait=False) == 0


def test_start_reports_a_command_that_failed(unreachable: None) -> None:
    assert start(SentinelConfig(start_command=["false"]), wait=False) == 2


def test_start_reports_a_command_that_is_not_there(unreachable: None) -> None:
    """The commands are yours; this one does not exist, and saying so
    beats a traceback about ENOENT."""
    assert start(SentinelConfig(start_command=["no-such-binary-here"]), wait=False) == 2


def test_stop_runs_the_configured_command() -> None:
    assert stop(SentinelConfig(stop_command=["true"])) == 0
    assert stop(SentinelConfig(stop_command=["false"])) == 2


def test_a_command_is_argv_never_a_shell_string(
    unreachable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No shell is involved, so a metacharacter in a config-supplied
    command reaches the program as a literal argument."""
    seen: dict[str, Any] = {}

    class Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv: list[str], **kw: Any) -> Done:
        seen["argv"] = argv
        seen["shell"] = kw.get("shell", False)
        return Done()

    monkeypatch.setattr(backend_mod.subprocess, "run", fake_run)
    start(SentinelConfig(start_command=["echo", "a; rm -rf /"]), wait=False)

    assert seen["argv"] == ["echo", "a; rm -rf /"]
    assert seen["shell"] is False


# --- argument resolution ---------------------------------------------------


def test_a_leading_tilde_is_expanded() -> None:
    """No shell runs these, so ``~`` would otherwise reach the program
    literally and, for a volume mount, create a directory called ``~``."""
    (expanded,) = _resolve(["~/models"])
    assert expanded == os.path.expanduser("~/models")
    assert not expanded.startswith("~")


def test_a_dollar_sign_is_left_alone() -> None:
    """An argument containing one is likelier to mean something to the
    program than to be an environment variable."""
    assert _resolve(["--served-model-name=$MODEL"]) == ["--served-model-name=$MODEL"]


# --- The metrics reader and the monitor -------------------------------------
#
# The rule under all of these is the one the tool keeps everywhere: a thing
# nobody could see must not render as a thing that was not happening. Most
# backends this tool supports publish no metrics at all, so "unavailable" is
# the common case rather than the error case, and it has to stay legible.


_VLLM_SAMPLE = """
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="qwen3-8b-fp8"} 3.0
vllm:num_requests_waiting{engine="0",model_name="qwen3-8b-fp8"} 2.0
vllm:kv_cache_usage_perc{engine="0",model_name="qwen3-8b-fp8"} 0.0742
vllm:prefix_cache_queries_total{engine="0",model_name="qwen3-8b-fp8"} 1000.0
vllm:prefix_cache_hits_total{engine="0",model_name="qwen3-8b-fp8"} 910.0
vllm:num_preemptions_total{engine="0",model_name="qwen3-8b-fp8"} 0.0
vllm:request_success_total{engine="0",finished_reason="stop",model_name="q"} 282.0
vllm:request_success_total{engine="0",finished_reason="length",model_name="q"} 138.0
vllm:request_success_total{engine="0",finished_reason="abort",model_name="q"} 0.0
"""


def test_parse_metrics_reads_a_real_exposition_body() -> None:
    m = backend_mod.parse_metrics(_VLLM_SAMPLE)
    assert m.running == 3.0
    assert m.waiting == 2.0
    assert m.kv_cache_used == pytest.approx(0.0742)
    assert m.finished == 420.0
    assert m.truncated == 138.0
    assert m.prefix_hit_rate == pytest.approx(0.91)


def test_parse_metrics_accepts_the_older_spellings() -> None:
    """A reader pinned to one release's names reports a live server as silent."""
    older = (
        'vllm:gpu_cache_usage_perc{engine="0"} 0.5\n'
        'vllm:gpu_prefix_cache_hits_total{engine="0"} 5.0\n'
        'vllm:gpu_prefix_cache_queries_total{engine="0"} 10.0\n'
    )
    m = backend_mod.parse_metrics(older)
    assert m.kv_cache_used == 0.5
    assert m.prefix_hit_rate == 0.5


def test_parse_metrics_leaves_what_it_did_not_see_as_none() -> None:
    """Absent is not zero — the whole point of the model's optional fields."""
    m = backend_mod.parse_metrics('vllm:num_requests_running{engine="0"} 1.0\n')
    assert m.running == 1.0
    assert m.waiting is None
    assert m.kv_cache_used is None
    assert m.truncated is None, "no request_success_total means no count, not 0"
    assert m.prefix_hit_rate is None


def test_parse_metrics_survives_junk() -> None:
    m = backend_mod.parse_metrics("garbage\n\n# comment\nvllm:num_requests_running x\n")
    assert not m.anything


def test_a_zero_denominator_is_not_a_rate() -> None:
    m = BackendMetrics(prefix_hits=0.0, prefix_queries=0.0)
    assert m.prefix_hit_rate is None


def test_no_metrics_endpoint_does_not_render_as_an_idle_server() -> None:
    lines = backend_mod.render_metrics(None)
    body = " ".join(lines)
    assert "publishes no /metrics" in body
    assert "not zero" in body
    assert "0" not in body.replace("/metrics", ""), (
        "no zeroed rows for an unseen server"
    )


def test_an_unrecognised_body_is_its_own_state() -> None:
    """Answered-with-nothing-we-know is not the same as has-no-endpoint."""
    lines = backend_mod.render_metrics(BackendMetrics())
    body = " ".join(lines)
    assert "nothing this reader knows" in body
    assert "publishes no /metrics" not in body


def test_unavailable_fields_say_so_beside_ones_that_answered() -> None:
    lines = backend_mod.render_metrics(BackendMetrics(running=2.0))
    body = "\n".join(lines)
    assert "requests running    2" in body
    assert "unavailable" in body


def test_a_queue_and_a_truncation_are_called_out() -> None:
    """Both are the reader's cue that a setting is wrong, so both are named."""
    lines = backend_mod.render_metrics(
        BackendMetrics(waiting=4.0, truncated=7.0, finished=10.0)
    )
    body = "\n".join(lines)
    assert "wider than the server will batch" in body
    assert "retry at double the budget" in body


def test_nothing_is_called_out_when_there_is_nothing_to_call_out() -> None:
    lines = backend_mod.render_metrics(
        BackendMetrics(waiting=0.0, truncated=0.0, finished=10.0)
    )
    body = "\n".join(lines)
    assert "wider than the server" not in body
    assert "retry at double" not in body
