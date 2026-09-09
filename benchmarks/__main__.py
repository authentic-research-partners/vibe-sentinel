"""CLI for the benchmarks: ``python -m benchmarks <suite>``.

    python -m benchmarks                 every suite below
    python -m benchmarks concurrency     what the fan-out width buys
    python -m benchmarks prefix          what the identical system message buys
    python -m benchmarks client          whether a client per call costs anything
    python -m benchmarks truncation      what an answer over its budget costs

    python -m benchmarks --repeat 1      one pass instead of three, for a look
    python -m benchmarks --format json   the run as one JSON object

Every suite needs a backend. There is no mechanical tier here and there
is no ``--no-model``: an eval can measure the preparation without one
because the preparation is arithmetic, and a duration cannot be measured
without the thing whose duration it is.

The one ``asyncio.run`` in this package is here, for the reason the tool
keeps the same rule: a function that opens its own loop raises the moment
something already inside one calls it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from benchmarks import harness as bench_harness
from benchmarks import report as render
from benchmarks import suites as bench
from vibe_sentinel.config import SentinelConfig, load_config
from vibe_sentinel.exceptions import LLMConnectionError

#: Where a run is written unless ``--output-dir`` says otherwise. Beside
#: the code that produced it, because the point of a duration is
#: comparing it with the last one on the same card.
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "results"

SUITES = ("concurrency", "prefix", "client", "truncation")


def _widths(raw: str) -> tuple[int, ...]:
    try:
        values = tuple(int(part) for part in raw.split(",") if part.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--widths takes a comma-separated list of integers, not {raw!r} "
            f"(e.g. --widths 1,4,16)"
        ) from None
    if not values or any(v < 1 for v in values):
        raise argparse.ArgumentTypeError(
            f"--widths must name at least one width of 1 or more, not {raw!r}"
        )
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks",
        description=(
            "Measure what vibe-sentinel's model traffic costs on your backend. "
            "Reports durations, gates nothing."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "suite",
        nargs="?",
        default="all",
        choices=(*SUITES, "all"),
        help="which suite to run (default: all)",
    )
    parser.add_argument(
        "--requests",
        type=int,
        default=bench.REQUESTS,
        help=(
            f"requests in one pass, sized against a real fan-out "
            f"(default: {bench.REQUESTS})"
        ),
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=3,
        help=(
            "measured passes per condition; the median is reported and a "
            "warm-up runs first either way (default: 3)"
        ),
    )
    parser.add_argument(
        "--widths",
        type=_widths,
        default=bench.WIDTHS,
        help=(
            f"fan-out widths the concurrency suite sweeps, comma-separated "
            f"(default: {','.join(str(w) for w in bench.WIDTHS)})"
        ),
    )
    parser.add_argument(
        "--width",
        type=int,
        default=bench.DEFAULT_WIDTH,
        help=(
            f"fan-out width the prefix and client suites hold fixed — the "
            f"shipped concurrency default (default: {bench.DEFAULT_WIDTH})"
        ),
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=bench_harness.BENCH_MAX_TOKENS,
        help=(
            f"answer budget held fixed across every condition, so answer "
            f"length is not a second variable (default: "
            f"{bench_harness.BENCH_MAX_TOKENS})"
        ),
    )
    parser.add_argument(
        "--note",
        default="",
        help="a line recorded with the run, e.g. the serving stack and its flags",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="where the markdown and JSON records are written (default: results/)",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="print the run without recording it",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="stdout rendering: a table for a person, or the whole run as JSON",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="config file to read [llm] from (default: ./.vibe-sentinel.toml)",
    )
    return parser


async def _run(args: argparse.Namespace, config: SentinelConfig) -> render.Run:
    chosen = SUITES if args.suite == "all" else (args.suite,)
    # One budget for every request in every suite. See BENCH_MAX_TOKENS.
    config = config.model_copy(update={"max_tokens": args.max_tokens})
    run = render.Run(
        environment=await render.describe(config, note=args.note),
        requests=args.requests,
        repeat=args.repeat,
    )
    for name in chosen:
        if name == "concurrency":
            run.suites.append(
                await bench.concurrency(
                    config, args.requests, args.repeat, tuple(args.widths)
                )
            )
        elif name == "prefix":
            run.suites.append(
                await bench.prefix(config, args.requests, args.repeat, args.width)
            )
        elif name == "client":
            run.suites.append(
                await bench.client(config, args.requests, args.repeat, args.width)
            )
        else:
            run.suites.append(
                await bench.truncation(config, args.requests, args.repeat, args.width)
            )
    return run


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.repeat < 1 or args.requests < 1:
        print("--repeat and --requests must both be at least 1", file=sys.stderr)
        return 2

    config = load_config(args.config)
    try:
        run = asyncio.run(_run(args, config))
    except LLMConnectionError as e:
        # The endpoint is down. Same failure the tool itself gives, with
        # the same remediation: a benchmark that reported a duration for
        # requests nobody answered would be reporting on nothing.
        print(str(e), file=sys.stderr)
        return 2
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    print(render.to_json(run) if args.format == "json" else render.render(run))
    if not args.no_write:
        for path in render.write(run, args.output_dir):
            print(f"  written: {path.name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
