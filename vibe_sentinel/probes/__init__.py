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
import os
from collections.abc import Sequence
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


def unreadable_dirs(root: Path) -> list[str]:
    """Directories under ``root`` that could not be listed.

    ``Path.rglob`` swallows a ``PermissionError`` and keeps going, so an
    unreadable subtree is indistinguishable from one that is not there:
    its files are never yielded, never counted as skipped, and every
    total comes back smaller and perfectly confident. The day the
    permission is fixed they arrive as drift that never happened.

    Walked separately from :func:`iter_source_files` rather than folded
    into it, so that function's glob semantics stay exactly ``rglob``'s.
    Excluded directories are pruned before descent, so a virtualenv
    nobody can read is not a finding about this project.
    """
    failures: list[str] = []

    def note(error: OSError) -> None:
        name = error.filename or ""
        try:
            failures.append(Path(name).relative_to(root).as_posix())
        except ValueError:
            failures.append(name)

    for _current, subdirs, _files in os.walk(root, onerror=note):
        subdirs[:] = [
            d for d in subdirs if d not in EXCLUDED_DIRS and not d.endswith(".egg-info")
        ]
    return sorted(set(failures))


#: The key every probe reports what it could not measure under. One key,
#: one number, the same meaning in each probe — so a reader who learns it
#: once knows it everywhere, and so the series is comparable across them.
NOT_MEASURED_KEY = "not-measured"


def not_measured(
    skipped: Sequence[str], unreadable: Sequence[str] = ()
) -> dict[str, Any]:
    """The observation for what was in scope and could not be read.

    Every probe emits this, always, even at zero. Keyed and always
    present makes it a series rather than a note: a tree that starts
    failing to parse moves it off zero, and these probes carry
    ``tolerance = 0``, so that surfaces on the scan it happens.

    Reported rather than raised, and the distinction is the same one
    ``nothing_measured`` is on the other side of. A probe that measured
    *nothing* fails, because there is no aggregate left for a
    qualification to attach to. A probe that measured most of a tree
    still has numbers worth keeping — and a repository holding one
    permanently unparseable file would otherwise fail that probe on every
    scan for ever, with nothing anyone could do to clear it, which is the
    report-forever-with-no-decision shape this codebase has already been
    wrong about once.

    What it must never be is silent. The counts these probes emit are
    sums over the files they could read, and a sum over an unknown subset
    is not a measurement of the tree unless the gap is on the record
    beside it.
    """
    missing = list(skipped) + [f"{d}/" for d in unreadable]
    shown = ", ".join(missing[:3])
    more = f" and {len(missing) - 3} more" if len(missing) > 3 else ""
    # Leads with the key's own name because `compare()` renders an
    # observation's label as the change line, and "every path was read"
    # arriving on its own reads as a finding about the code.
    if missing:
        label = f"not measured: {len(missing)} path(s) — {shown}{more}"
    else:
        label = "not measured: none — every path in scope was read"
    return {
        "key": NOT_MEASURED_KEY,
        "value": float(len(missing)),
        "label": label,
        "attrs": {
            "unparsed": str(len(skipped)),
            "unreadable_dirs": str(len(unreadable)),
        },
    }


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
