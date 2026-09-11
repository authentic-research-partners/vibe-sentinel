"""Starting, stopping, and checking the model backend.

Deliberately thin. vibe-sentinel does not manage containers, know model
registries, or reason about GPU memory — it runs the command you put in
your config and probes the endpoint over HTTP.

That is the whole difference between "works with vLLM" and "works with
whatever you already run". The commands below are yours; nothing here
parses or validates them beyond refusing to involve a shell.

:func:`read_metrics` is the one thing here that looks past the
OpenAI-compatible surface, and it is a *diagnostic*, never a dependency.
Some servers publish a Prometheus ``/metrics`` endpoint saying how deep
their queue is and how much of the KV cache is in use; most of the
backends this tool supports — Ollama, llama.cpp, LM Studio — publish
nothing of the kind, and a scan neither reads it nor cares. So every
field it returns is optional, an endpoint that is not there is not an
error, and a backend that publishes nothing renders as *unavailable*
rather than as a server with an empty queue. Nothing in the scan path
calls it.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time

import httpx
from loguru import logger

from vibe_sentinel.config import SentinelConfig
from vibe_sentinel.llm import check_endpoint
from vibe_sentinel.schemas import BackendMetrics

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
    if config.env_overrides:
        # Named, not merely flagged. The failure this command exists to
        # catch is a backend answering under the expected name while being
        # something else, and "some of this came from elsewhere" would be
        # one more way to read the wrong line and believe it.
        print(f"Source:   {', '.join(config.env_overrides)} (environment)")

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


#: Prometheus metric names this reader understands, newest spelling first.
#: vLLM renamed ``gpu_cache_usage_perc`` to ``kv_cache_usage_perc`` and
#: replaced a ready-made ``gpu_prefix_cache_hit_rate`` gauge with the two
#: counters it was computed from, so a reader pinned to one spelling reports
#: a live server as silent. Unknown names are left alone rather than guessed.
_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "running": ("vllm:num_requests_running",),
    "waiting": ("vllm:num_requests_waiting",),
    "kv_cache_used": ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"),
    "prefix_hits": ("vllm:prefix_cache_hits_total", "vllm:gpu_prefix_cache_hits_total"),
    "prefix_queries": (
        "vllm:prefix_cache_queries_total",
        "vllm:gpu_prefix_cache_queries_total",
    ),
    "preemptions": ("vllm:num_preemptions_total",),
}

#: The counter that says an answer was cut off at ``max_tokens``. Split by a
#: label rather than given its own metric, so it is summed separately below.
_SUCCESS_METRIC = "vllm:request_success_total"
_TRUNCATED_LABEL = 'finished_reason="length"'

#: Seconds to wait for /metrics. Short: this is a diagnostic beside a live
#: server, and a monitor that hangs is worse than one that says it could not
#: read this tick.
_METRICS_TIMEOUT_S = 5.0


def parse_metrics(text: str) -> BackendMetrics:
    """Parse a Prometheus exposition body into the fields we understand.

    Generic on purpose — ``name{labels} value`` is the whole format, and
    nothing here knows which server wrote it. A name we do not recognise is
    skipped, and a name we do recognise but cannot parse as a number leaves
    its field ``None``, which renders as *unavailable*.
    """
    wanted = {name: field for field, names in _METRIC_ALIASES.items() for name in names}
    found: dict[str, float] = {}
    finished = 0.0
    truncated = 0.0
    saw_success = False

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, rest = line.partition("{")
        if rest:
            labels, _, value = rest.partition("}")
        else:
            labels = ""
            name, _, value = line.partition(" ")
        try:
            number = float(value.strip())
        except ValueError:
            continue

        if name == _SUCCESS_METRIC:
            saw_success = True
            finished += number
            if _TRUNCATED_LABEL in labels:
                truncated += number
            continue

        field = wanted.get(name)
        # First spelling wins: the alias tuples are ordered, and a server
        # publishing both must not have the older one overwrite the newer.
        if field is not None and field not in found:
            found[field] = number

    return BackendMetrics(
        **found,
        finished=finished if saw_success else None,
        truncated=truncated if saw_success else None,
    )


async def read_metrics(config: SentinelConfig) -> BackendMetrics | None:
    """Read the backend's Prometheus metrics. ``None`` when it publishes none.

    ``None`` and an all-``None`` :class:`BackendMetrics` mean different
    things and both are real: the first is "there is no /metrics here", the
    second is "there is one and it said nothing we understand". Neither is an
    error — most supported backends have no such endpoint — so this never
    raises and never logs above debug.
    """
    # /metrics sits at the server root, beside /v1, not under it.
    base = config.llm_endpoint.rstrip("/").removesuffix("/v1")
    url = f"{base}/metrics"
    headers = {}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    try:
        async with httpx.AsyncClient(timeout=_METRICS_TIMEOUT_S) as client:
            response = await client.get(url, headers=headers)
    except (httpx.TimeoutException, httpx.TransportError) as e:
        logger.debug("no metrics endpoint at {}: {}", url, e)
        return None
    if response.status_code >= 400:
        logger.debug("metrics endpoint at {} returned {}", url, response.status_code)
        return None
    return parse_metrics(response.text)


def _cell(value: float | None, unit: str = "", digits: int = 0) -> str:
    """One metric, or ``unavailable``. Never a zero standing in for one."""
    if value is None:
        return "unavailable"
    if unit == "%":
        return f"{value * 100:.1f}%"
    return f"{value:,.{digits}f}{unit}"


def render_metrics(metrics: BackendMetrics | None) -> list[str]:
    """The monitor's body: what the backend says, or why there is nothing.

    Three states, kept apart on purpose. No ``/metrics`` endpoint at all —
    which is every backend but vLLM among the ones this tool supports, and
    not a fault. An endpoint that answered with nothing recognisable, which
    usually means a version that renamed its metrics. And real numbers. A
    renderer that collapsed the first two into rows of zeroes would report
    a server nobody can see as a server with nothing happening.
    """
    if metrics is None:
        return [
            "  This backend publishes no /metrics endpoint.",
            "  Queue depth, KV cache use and prefix-cache hits are unavailable",
            "  here — not zero. Ollama, llama.cpp and LM Studio expose none of",
            "  them; vLLM does, and the scan needs none of it either way.",
        ]
    if not metrics.anything:
        return [
            "  The /metrics endpoint answered with nothing this reader knows.",
            "  Metric names move between releases; the reader accepts the",
            "  spellings it knows and reports the rest as unavailable rather",
            "  than guessing.",
        ]
    return [
        f"  requests running    {_cell(metrics.running)}",
        f"  requests waiting    {_cell(metrics.waiting)}"
        + (
            "   <- queueing: the fan-out is wider than the server will batch"
            if metrics.waiting
            else ""
        ),
        f"  KV cache in use     {_cell(metrics.kv_cache_used, '%')}",
        f"  prefix cache hits   {_cell(metrics.prefix_hit_rate, '%')}"
        + (
            f"  ({_cell(metrics.prefix_hits)} of {_cell(metrics.prefix_queries)})"
            if metrics.prefix_hits is not None
            else ""
        ),
        f"  preemptions         {_cell(metrics.preemptions)}",
        f"  answers finished    {_cell(metrics.finished)}",
        f"  cut off at the      {_cell(metrics.truncated)}"
        + (
            "   <- each one costs a retry at double the budget"
            if metrics.truncated
            else ""
        ),
        "    token ceiling",
    ]


async def monitor(config: SentinelConfig, interval: float, count: int) -> int:
    """Print what the backend is doing, every ``interval`` seconds.

    A diagnostic, and shaped like one: it reads, it never writes, and it
    cannot change what a scan does. Exit code says whether the endpoint
    answered on the last tick, so it is usable in a shell test as well as
    watched by a person.

    The counters here are cumulative since the server started, which is
    what makes two ticks more useful than one: the interesting quantities
    are differences. ``benchmarks/`` reads the same numbers either side of
    a timed pass for exactly that reason.
    """
    last = 1
    for tick in range(count):
        reachable, detail = await check_endpoint(config)
        metrics = await read_metrics(config) if reachable else None
        lines = [
            "",
            "=" * 68,
            f"  {config.llm_endpoint}   {time.strftime('%H:%M:%S')}",
            "=" * 68,
            f"  model configured    {config.llm_model}",
            f"  endpoint            {'ready' if reachable else 'NOT REACHABLE'} — {detail}",
        ]
        if reachable:
            lines += ["", *render_metrics(metrics)]
        else:
            lines += [
                "",
                "  Start it with:  vibe-sentinel backend start",
                "  A scan cannot run without it: the model is not optional.",
            ]
        lines.append("")
        print("\n".join(lines))
        last = 0 if reachable else 1
        if tick + 1 < count:
            await asyncio.sleep(interval)
    return last
