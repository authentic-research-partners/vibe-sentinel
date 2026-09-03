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
