"""Probe scripts shipped with the package.

Each is a standalone ``python -m vibe_sentinel.probes.<name>`` command
that prints one JSON object to stdout:

    {"observations": [{"key": ..., "value": ..., "label": ...}],
     "summary": "..."}

They are ordinary commands, not plugins — a probe template invokes them
the same way it would invoke ``ast-grep``, ``wc``, or a company's own
script. Nothing here is privileged; they exist so the tool does something
useful out of the box and so there are worked examples to copy when
writing your own.

  - ``comments``    — commentary vs code, per package.
  - ``modules``     — where code lives and how modules load each other.
  - ``handlers``    — what error handlers do with the error.
  - ``patterns``    — a census of code-organization patterns (ast-grep).
  - ``length``      — how long files are, in categories you name.

Licences, dependency provenance and credentials are not here. They are
gates — see :mod:`vibe_sentinel.gates` — because what they report is a
state rather than a transition.

The set that actually runs is whatever ``probes.default.toml`` declares
layered with the project's own ``[[probe]]`` tables, not this list.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


#: Directories no probe measures, whatever root it is pointed at.
#:
#: Not a convenience. A probe told to measure ``.`` in a project with a
#: virtualenv at the root will otherwise walk it, and dependency source
#: outnumbers project source by two orders of magnitude — this repository
#: baselined 3,143 ``.venv`` observations against 74 of its own before
#: this list existed. The result is not merely noisy: every dependency
#: upgrade then reads as structural drift in *your* codebase, which is
#: the one thing these numbers must never say.
#:
#: Matched on directory name at any depth, so ``src/node_modules`` goes
#: too. A project that genuinely keeps source in a directory named
#: ``build`` should point ``SOURCE_ROOT`` at it directly.
EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        ".tox",
        ".nox",
        "site-packages",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".ipynb_checkpoints",
        "build",
        "dist",
        ".eggs",
        ".vibe-sentinel",
    }
)


def is_excluded(path: Path, root: Path | None = None) -> bool:
    """True when any directory on ``path`` is one no probe measures.

    ``root`` is stripped first where given, so a project that genuinely
    lives under ``/srv/build/app`` is not excluded by its own prefix —
    only what lies *inside* the measured tree counts.
    """
    try:
        relative = path.relative_to(root) if root is not None else path
    except ValueError:
        relative = path
    parts = relative.parts
    # The file's own name is not a directory, so it never excludes.
    return any(part in EXCLUDED_DIRS for part in parts[:-1]) or any(
        part.endswith(".egg-info") for part in parts[:-1]
    )


def iter_source_files(root: Path, glob: str) -> list[Path]:
    """Every file under ``root`` matching ``glob``, skipping what is not source.

    Sorted, so a probe's output order is a function of the tree rather
    than of the filesystem's.
    """
    return sorted(
        path
        for path in root.rglob(glob)
        if path.is_file() and not is_excluded(path, root)
    )


def emit(observations: list[dict[str, Any]], summary: str = "") -> None:
    """Print the probe protocol's JSON object to stdout."""
    print(json.dumps({"observations": observations, "summary": summary}, indent=2))


def nothing_measured(
    prog: str, root: Path, glob: str, matched: int, parsed: int
) -> str | None:
    """Why this probe measured nothing, or None if it measured something.

    ``comments``, ``modules`` and ``handlers`` all read source through
    :mod:`ast`, so a tree in another language does not fail file by file
    in a way anyone notices — every file is skipped to stderr and the
    probe emits zero
    observations. Zero observations is a legitimate answer (an empty
    package), which is exactly the problem: pointed at a TypeScript tree
    these probes report the same thing they report for clean Python, and
    the next comparison reads it as "nothing here" rather than "wrong
    tool". A measurement that was never taken must not be recorded as a
    measurement of zero.

    So both cases are errors with the remedy in them, and the runner
    records the probe as failed — which is loud in the report and, unlike
    an empty result, cannot be mistaken for a finding.
    """
    if matched == 0:
        return (
            f"probe {prog}: --glob {glob!r} matched no files under {root}. "
            f"Check --root and --glob: with no matches this probe would "
            f"otherwise report zero, which is indistinguishable from a "
            f"directory that really is empty."
        )
    if parsed == 0:
        return (
            f"probe {prog}: none of the {matched} file(s) matching {glob!r} "
            f"under {root} could be parsed as Python. This probe measures "
            f"Python only. For another language use the pattern-census "
            f"probe, which takes --lang and runs on anything ast-grep parses."
        )
    return None
