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


def resolve_ast_grep(timeout: float = 10.0) -> str:
    """The ast-grep binary on PATH, confirmed to be ast-grep.

    ``sg`` is ast-grep's own short name and is also shadow-utils' setgid
    utility, which ships at ``/usr/bin/sg`` on most Linux distributions.
    Taking the first ``sg`` on PATH is therefore a coin toss, and losing
    it is silent: that one answers ``sg run --pattern ...`` with "group
    'run' does not exist", exit 1 and nothing on stdout — which is
    exactly how ast-grep reports finding no matches. The probe would
    record a confident zero for a measurement that never ran, and the
    count would jump to its real value the first time it ran somewhere
    with the right binary, as drift that never happened.

    So the name is not enough: the binary has to say what it is.
    """
    for name in ("ast-grep", "sg"):
        binary = shutil.which(name)
        if binary is None:
            continue
        try:
            proc = subprocess.run(  # noqa: S603  # fixed argv, no shell
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0 and "ast-grep" in proc.stdout.lower():
            return binary
    raise RuntimeError(
        "ast-grep not found on PATH. Install it with: pip install ast-grep-cli. "
        "An `sg` that is not ast-grep — shadow-utils' setgid tool, at "
        "/usr/bin/sg on most Linux distributions — is ignored rather than run."
    )


def run_ast_grep(pattern: str, lang: str, root: Path, timeout: float) -> list[dict]:
    """Return ast-grep's JSON matches for ``pattern`` under ``root``."""
    binary = resolve_ast_grep()
    result = subprocess.run(  # noqa: S603  # fixed argv, no shell
        [binary, "run", "--pattern", pattern, "--lang", lang, "--json", str(root)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    # A path ast-grep could not read is reported on stderr and does not
    # change the exit code: an unreadable subdirectory gives "ERROR:
    # ./locked: Permission denied (os error 13)", exit 0, and valid JSON
    # for everything else. Returning that count would record a partial
    # scan as a complete one, and the missing matches would arrive as
    # drift the day the permission is fixed. A count that did not cover
    # the tree is not a count, so the probe fails and says which path.
    errors = [
        line for line in result.stderr.splitlines() if line.strip().startswith("ERROR")
    ]
    if errors:
        raise RuntimeError(
            f"ast-grep could not read part of {root}, so the count would be "
            f"short: {'; '.join(errors)[:400]}"
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
    # Summed from the filtered per-directory counts, never from `matches`.
    # ast-grep was given the whole root, so `matches` still holds every
    # hit inside the virtualenv — and a total that counts the
    # dependencies turns each upgrade of them into drift in this
    # codebase, which is the one thing EXCLUDED_DIRS exists to prevent.
    total = sum(per_dir.values())
    observations.append(
        {
            "key": "pattern-total",
            "value": float(total),
            "label": f"{total} total match(es) for {args.pattern!r}",
            "attrs": {"pattern": args.pattern, "directories": str(len(per_dir))},
        }
    )

    emit(
        observations,
        summary=(
            f"{total} match(es) for {args.pattern!r} across "
            f"{len(per_dir)} director{'y' if len(per_dir) == 1 else 'ies'}"
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
