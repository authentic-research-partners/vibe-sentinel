"""Starting, stopping, and checking the model backend.

Deliberately thin. vibe-sentinel does not manage containers, know model
registries, or reason about GPU memory — it runs the command you put in
your config and probes the endpoint over HTTP.

That is the whole difference between "works with vLLM" and "works with
whatever you already run". The commands below are yours; nothing here
parses or validates them beyond refusing to involve a shell.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time

from loguru import logger

from vibe_sentinel.config import SentinelConfig
from vibe_sentinel.llm import check_endpoint

#: How long ``start`` waits for the endpoint to answer before giving up.
#: Generous — a cold backend loads weights from disk on first start.
_READY_TIMEOUT_S = 300
_READY_POLL_S = 3.0


def _resolve(command: list[str]) -> list[str]:
    """Expand a leading ``~`` in each argument.

    No shell runs these commands, so ``~`` would otherwise reach the
    program literally and, for a volume mount, silently create a
    directory named ``~``. Only home expansion is done — not ``$VAR`` —
    because a command argument containing a dollar sign is more likely
    to be meaningful to the program than to be an environment variable.
    """
    return [os.path.expanduser(part) for part in command]


async def status(config: SentinelConfig) -> int:
    """Report whether the endpoint answers, and what it serves."""
    print(f"Endpoint: {config.llm_endpoint}")
    print(f"Model:    {config.llm_model}")
    print(f"Output:   {config.structured_output}")

    reachable, detail = await check_endpoint(config)
    if reachable:
        print(f"Status:   ready — {detail}")
        return 0

    print(f"Status:   unreachable — {detail}")
    if config.start_command:
        print("\nStart it with: vibe-sentinel backend start")
    else:
        print(
            "\nNo [llm] start_command configured — start your backend "
            "yourself, or add one to .vibe-sentinel.toml."
        )
    return 1


async def start(config: SentinelConfig, wait: bool = True) -> int:
    """Run the configured start command and wait for the endpoint."""
    if not config.start_command:
        logger.error(
            "No [llm] start_command in {}. Either start your backend "
            "yourself, or add the command — for example:\n"
            '  start_command = ["ollama", "serve"]',
            config.config_path or "the config",
        )
        return 2

    reachable, detail = await check_endpoint(config)
    if reachable:
        print(f"Already running at {config.llm_endpoint} — {detail}")
        return 0

    command = _resolve(config.start_command)
    logger.info("starting backend: {}", " ".join(command))
    try:
        result = subprocess.run(  # noqa: S603  # config-supplied argv, no shell
            command, capture_output=True, text=True, check=False
        )
    except OSError as e:
        logger.error(
            "Could not run start_command: {}\n  command: {}",
            e,
            " ".join(command),
        )
        return 2

    if result.returncode != 0:
        logger.error(
            "start_command exited {}: {}",
            result.returncode,
            result.stderr.strip()[:500] or "(no stderr)",
        )
        return 2
    if result.stdout.strip():
        print(result.stdout.strip())

    if not wait:
        return 0

    print(f"Waiting for {config.llm_endpoint} (up to {_READY_TIMEOUT_S}s)...")
    deadline = time.monotonic() + _READY_TIMEOUT_S
    while time.monotonic() < deadline:
        reachable, detail = await check_endpoint(config)
        if reachable:
            print(f"Ready — {detail}")
            return 0
        await asyncio.sleep(_READY_POLL_S)

    logger.error(
        "Backend did not become ready within {}s. It may still be loading — "
        "check its own logs, then: vibe-sentinel backend status",
        _READY_TIMEOUT_S,
    )
    return 2


def stop(config: SentinelConfig) -> int:
    """Run the configured stop command."""
    if not config.stop_command:
        logger.error(
            "No [llm] stop_command in {}. Stop your backend yourself, or "
            "add the command to the config.",
            config.config_path or "the config",
        )
        return 2

    command = _resolve(config.stop_command)
    logger.info("stopping backend: {}", " ".join(command))
    try:
        result = subprocess.run(  # noqa: S603  # config-supplied argv, no shell
            command, capture_output=True, text=True, check=False
        )
    except OSError as e:
        logger.error("Could not run stop_command: {}", e)
        return 2

    if result.returncode != 0:
        logger.error(
            "stop_command exited {}: {}",
            result.returncode,
            result.stderr.strip()[:500] or "(no stderr)",
        )
        return 2
    if result.stdout.strip():
        print(result.stdout.strip())
    return 0
