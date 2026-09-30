"""CLI for the evals: ``python -m evals <suite>``.

    python -m evals safety              the gate in front of every tool call
    python -m evals credentials         secrets at rest, and which are real
    python -m evals packages            names one edit apart
    python -m evals drift               what a lens makes of a report
    python -m evals adversarial         the attacks in the threat model
    python -m evals all                 every suite above

    python -m evals all --mechanical    only what needs no model
    python -m evals safety --repeat 3   how stable the verdicts are
    python -m evals all --format json   the suites as one JSON object

The one ``asyncio.run`` in the eval framework is here, for the reason the
package keeps the same rule: nothing below an entry point opens a loop,
because a function that opens its own raises the moment something already
inside one calls it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from evals import (
    adversarial_eval,
    credentials_eval,
    drift_eval,
    metrics,
    packages_eval,
    report,
    safety_eval,
)
from vibe_sentinel.config import DEFAULT_CONCURRENCY, SentinelConfig, load_config

#: Where a run is written unless ``--output-dir`` says otherwise. The
#: point of a rate is comparing it with the last one.
DEFAULT_OUTPUT = Path("results")

SUITES = ("safety", "credentials", "packages", "drift", "adversarial")


async def _run_suite(
    name: str, config: SentinelConfig, args: argparse.Namespace
) -> metrics.Suite:
    if name == "safety":
        return await safety_eval.run(
            config, mechanical_only=args.mechanical, concurrency=args.concurrency
        )
    if name == "credentials":
        return await credentials_eval.run(config, mechanical_only=args.mechanical)
    if name == "packages":
        return await packages_eval.run(config, mechanical_only=args.mechanical)
    if name == "adversarial":
        return await adversarial_eval.run(
            config, mechanical_only=args.mechanical, concurrency=args.concurrency
        )
    return await drift_eval.run(config, mechanical_only=args.mechanical)


async def _run(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config) if args.config else None)
    wanted = SUITES if args.suite == "all" else (args.suite,)

    runs: dict[str, list[metrics.Suite]] = {name: [] for name in wanted}
    for _ in range(max(1, args.repeat)):
        for name in wanted:
            try:
                runs[name].append(await _run_suite(name, config, args))
            except ValueError as e:
                # A malformed [[lens.case]] is a question this project meant
                # to ask and cannot. Same call the scan makes about a
                # malformed [[lens]]: stop, name the file and the entry, and
                # say what fixes it. stderr, so --format json stays parseable.
                print(f"evals: {e}", file=sys.stderr)
                return 2

    latest = [runs[name][-1] for name in wanted]
    if args.format == "json":
        print(report.as_json(latest))
    else:
        for suite in latest:
            print(report.render(suite, verbose=args.verbose))
        if args.repeat > 1:
            for name in wanted:
                print(report.render_spread(metrics.spread(runs[name])))
                rates = metrics.stability(runs[name])
                if rates:
                    print(report.render_stability(rates))

    if not args.no_write:
        out = Path(args.output_dir)
        written = [report.write(suite, out) for suite in latest]
        if args.format != "json":
            print(f"Written: {', '.join(str(p) for p in written)}")

    # A suite that could not reach a backend is not a failure of the
    # thing being measured, and an eval never fails a build on a rate.
    # The only non-zero exit is a suite that did not run at all.
    return 0 if all(s.measurements for s in latest) else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals",
        description=(
            "Measure the four judgements vibe-sentinel makes, against labelled corpora."
        ),
    )
    parser.add_argument(
        "suite",
        nargs="?",
        default="all",
        choices=(*SUITES, "all"),
        help="which suite to run (default: all)",
    )
    parser.add_argument(
        "--mechanical",
        action="store_true",
        help=(
            "measure only the stages that need no model — triage, the "
            "credential patterns, the near-miss pairing"
        ),
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help=(
            "run each suite this many times and report how often identical "
            "requests came back with the same label"
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=(
            "cases asked about at once in the safety suite. Each case's "
            "timeout counts the time it waits behind the others, so above 1 "
            "on a backend that answers one request at a time, the queue is "
            "reported as unanswered verdicts — the hook asks one at a time "
            f"(default: {DEFAULT_CONCURRENCY})"
        ),
    )
    parser.add_argument(
        "--format",
        default="terminal",
        choices=("terminal", "json"),
        help="terminal for a person, json for whatever compares two runs",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print every case that missed its label, not the first few",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT),
        help=f"where the JSON is written (default: {DEFAULT_OUTPUT}/)",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="print the result and write nothing",
    )
    parser.add_argument(
        "--config",
        help="path to .vibe-sentinel.toml (default: found by walking up)",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
