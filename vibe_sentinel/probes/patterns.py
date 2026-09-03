"""Probe: a census of code-organization patterns, via ast-grep.

Counts how often a structural pattern occurs and, more importantly,
*where*. The pattern itself is supplied by the template, so this probe is
the general-purpose escape hatch: anything ast-grep can express becomes a
tracked structural fact without new Python.

The drift value here is less about the count than about the locations. A
pattern that has only ever appeared in ``db/`` showing up in ``cli/`` is
a reorganization nobody announced, and it surfaces as a new observation
key rather than as a threshold breach.

Requires ``ast-grep`` on PATH (a dev dependency: ``ast-grep-cli``).

Usage::

    python -m vibe_sentinel.probes.patterns \\
        --root src --lang python --pattern 'sqlite3.connect($$$ARGS)'
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from vibe_sentinel.probes import emit, is_excluded


def run_ast_grep(pattern: str, lang: str, root: Path, timeout: float) -> list[dict]:
    """Return ast-grep's JSON matches for ``pattern`` under ``root``."""
    binary = shutil.which("ast-grep") or shutil.which("sg")
    if binary is None:
        raise RuntimeError(
            "ast-grep not found on PATH. Install it with: pip install ast-grep-cli"
        )
    result = subprocess.run(  # noqa: S603  # fixed argv, no shell
        [binary, "run", "--pattern", pattern, "--lang", lang, "--json", str(root)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    # ast-grep exits non-zero when there are no matches, which is a
    # legitimate result here, not an error. Only treat it as a failure
    # when stdout carries nothing parseable.
    out = result.stdout.strip()
    if not out:
        if result.returncode not in (0, 1):
            raise RuntimeError(
                f"ast-grep failed (exit {result.returncode}): "
                f"{result.stderr.strip()[:400]}"
            )
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ast-grep returned unparseable JSON: {e}") from e


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vibe_sentinel.probes.patterns",
        description="Count and locate a structural code pattern.",
    )
    parser.add_argument("--root", required=True, help="Directory to search")
    parser.add_argument("--pattern", required=True, help="ast-grep pattern")
    parser.add_argument("--lang", default="python", help="Language (default: python)")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"probe patterns: not a directory: {root}", file=sys.stderr)
        return 2

    try:
        matches = run_ast_grep(args.pattern, args.lang, root, args.timeout)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"probe patterns: {e}", file=sys.stderr)
        return 2

    # ast-grep is given the root and finds everything under it, including
    # the virtualenv. Filtered here rather than by passing it a list of
    # paths: one subprocess with one root stays one subprocess.
    per_dir: dict[str, int] = {}
    for m in matches:
        path = m.get("file") or m.get("path") or ""
        if path and is_excluded(Path(path), root):
            continue
        directory = Path(path).parent.as_posix() if path else "(unknown)"
        per_dir[directory] = per_dir.get(directory, 0) + 1

    observations = [
        {
            "key": f"pattern-in:{directory}",
            "value": float(count),
            "label": f"{directory}: {count} match(es)",
            "attrs": {"pattern": args.pattern},
        }
        for directory, count in sorted(per_dir.items())
    ]
    observations.append(
        {
            "key": "pattern-total",
            "value": float(len(matches)),
            "label": f"{len(matches)} total match(es) for {args.pattern!r}",
            "attrs": {"pattern": args.pattern, "directories": str(len(per_dir))},
        }
    )

    emit(
        observations,
        summary=(
            f"{len(matches)} match(es) for {args.pattern!r} across "
            f"{len(per_dir)} director{'y' if len(per_dir) == 1 else 'ies'}"
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
