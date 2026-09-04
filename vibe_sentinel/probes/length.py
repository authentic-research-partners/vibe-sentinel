"""Probe: how long files are, in categories the project names.

Length is the one measurement that needs no parser: it means the same
thing for a Python module and for CLAUDE.md. That is exactly why it has
to be reported per *category*. A repository whose line count grew by two
thousand has told you nothing — the question is whether that was the
package, the tests, the documentation, or the file the coding agent reads
before it starts, and those are four different problems.

The drift this catches is the one everybody feels and nobody measures.
CLAUDE.md was eighty lines when someone wrote it and is six hundred now,
a section at a time, none of them wrong. The README grew an FAQ entry per
session. One module became the place things get appended to. No per-line
rule fires on any of that, because length is the entire finding.

Categories are a parameter because no shipped list knows which files a
project treats as instructions. ``--categories`` takes
``name=glob,glob; name=glob`` and entries are tried in order: the first
that matches a file claims it. Specific filenames therefore go before the
globs that would swallow them — with ``docs=*.md`` first, ``CLAUDE.md``
is documentation and the category meant to watch it is empty.

One unit per run, and the unit is part of the observation key, so
changing it ends the old series instead of silently redefining every
point already recorded under it. ``lines`` is what most people mean by
how long a file is and is right for code; prose is where it is wrong,
because rewrapping a paragraph halves the line count of a file whose
content did not change. ``words`` and ``bytes`` survive a rewrap.
Whichever is chosen, the other two are recorded alongside it, so picking
one loses nothing.

Usage::

    python -m vibe_sentinel.probes.length --root . \\
        --categories 'agent=CLAUDE.md,AGENTS.md; docs=*.md; code=*.py'
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from vibe_sentinel.probes import (
    emit,
    iter_source_files,
    not_measured,
    unreadable_dirs,
)

#: What ``length`` can count. One per run — see the module docstring.
UNITS = ("lines", "words", "bytes")

#: A category name. Kept to what reads well in a report label and in a
#: ``[probes.parameters]`` line; the separators of the spec itself are
#: excluded so a missing ``;`` is an error rather than a category called
#: ``docs=*.md code``.
_NAME_RE = re.compile(r"^[A-Za-z][\w-]*$")


def parse_categories(spec: str) -> list[tuple[str, str]]:
    """``name=glob,glob; name=glob`` → ``(name, glob)`` pairs, in order.

    Order is the semantics rather than a formality: the first entry
    matching a file claims it. A name may appear more than once, which is
    how a category collects globs that have nothing in common —
    ``agent=CLAUDE.md,AGENTS.md`` is two entries under one name.

    Every failure raises rather than dropping the entry. A category
    silently skipped reports as a category that matched nothing, which
    reads as a project that has no documentation.
    """
    entries: list[tuple[str, str]] = []
    for raw in spec.split(";"):
        chunk = raw.strip()
        if not chunk:
            continue
        name, sep, globs = chunk.partition("=")
        name = name.strip()
        if not sep or _NAME_RE.match(name) is None:
            raise ValueError(
                f"category {chunk!r} is not `name=glob[,glob]`. A name starts "
                f"with a letter and holds letters, digits, - and _; entries "
                f"are separated by `;` and a category's globs by `,`."
            )
        patterns = [g.strip() for g in globs.split(",") if g.strip()]
        if not patterns:
            raise ValueError(
                f"category {name!r} declares no glob. Write "
                f"`{name}=<glob>`, e.g. `{name}=*.md`."
            )
        entries.extend((name, pattern) for pattern in patterns)
    if not entries:
        raise ValueError(
            "--categories is empty. Nothing would be measured, and an empty "
            "measurement is indistinguishable from an empty tree."
        )
    return entries


def measure(raw: bytes, unit: str) -> dict[str, int] | None:
    """Every length of one file's bytes that is defined for it, or None.

    For text, all three are computed whatever ``--unit`` says. They cost
    one pass each over a string already in memory, and the two that are
    not the unit are what makes a report line readable — "412 lines,
    3,100 words" answers "is this prose or is this code" without a second
    run.

    A binary file is the exception, and the reason ``unit`` is a
    parameter here. A PNG has a size; it does not have lines or words,
    and counting either would be inventing a number. So ``bytes`` is the
    one unit that measures one, and under any other unit a binary is
    still not text and is still skipped.

    That is what makes images measurable without widening anything.
    ``unit`` is part of the observation key, so a project watching what
    it ships declares a second, ``bytes``-unit probe with an image glob;
    the run that counts its source in lines is untouched, and no existing
    baseline gains a key — the shipped categories match no binary.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"bytes": len(raw)} if unit == "bytes" else None
    return {
        "lines": len(text.splitlines()),
        "words": len(text.split()),
        "bytes": len(raw),
    }


def nothing_measured(root: Path, spec: str, matched: int, read: int) -> str | None:
    """Why this probe measured nothing, or None if it measured something.

    The same rule the other probes keep: a measurement never taken must
    not be recorded as a measurement of zero. An empty observation list
    is what a correctly configured probe emits for a tree that holds none
    of these files, so a typo in ``--root`` or a glob that matches
    nothing would otherwise be recorded as a finding about the codebase.
    """
    if matched == 0:
        return (
            f"probe length: no file under {root} matched any category in "
            f"{spec!r}. Check --root and --categories: with no matches this "
            f"probe would otherwise report zero observations, which is "
            f"indistinguishable from a tree that holds none of these files."
        )
    if read == 0:
        return (
            f"probe length: none of the {matched} matched file(s) under "
            f"{root} could be read as UTF-8 text. This probe measures the "
            f"length of text; point each category at source or documentation "
            f"rather than at binaries."
        )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vibe_sentinel.probes.length",
        description="File length, per file, in categories you name.",
    )
    parser.add_argument("--root", required=True, help="Directory to measure")
    parser.add_argument(
        "--categories",
        required=True,
        help="`name=glob,glob; name=glob`, tried in order; first match wins",
    )
    parser.add_argument(
        "--unit", default="lines", choices=UNITS, help="What length means here"
    )
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"probe length: not a directory: {root}", file=sys.stderr)
        return 2
    try:
        entries = parse_categories(args.categories)
    except ValueError as e:
        print(f"probe length: {e}", file=sys.stderr)
        return 2

    unit: str = args.unit
    claimed: dict[Path, str] = {}
    for name, pattern in entries:
        try:
            found = iter_source_files(root, pattern)
        except (ValueError, NotImplementedError) as e:
            print(
                f"probe length: category {name!r} has an unusable glob "
                f"{pattern!r}: {e}. Globs are relative to --root and match at "
                f"any depth.",
                file=sys.stderr,
            )
            return 2
        for path in found:
            # First match wins, which is what makes the entry order the
            # way a project says CLAUDE.md is not just another *.md.
            claimed.setdefault(path, name)

    # Deduplicates names while keeping the order they were declared in,
    # so the summary reads the way the config does.
    files: dict[str, int] = {name: 0 for name, _ in entries}
    totals: dict[str, int] = dict.fromkeys(files, 0)

    observations: list[dict] = []
    skipped: list[str] = []
    skipped_in: dict[str, int] = dict.fromkeys(files, 0)
    longest_value, longest_path = -1, ""
    for path in sorted(claimed):
        try:
            raw = path.read_bytes()
        except OSError as e:
            print(f"skipped {path}: {e}", file=sys.stderr)
            skipped.append(path.as_posix())
            skipped_in[claimed[path]] += 1
            continue
        sizes = measure(raw, unit)
        if sizes is None:
            print(
                f"skipped {path}: not UTF-8 text, so it has no {unit}. "
                f"Measure binary files with --unit bytes.",
                file=sys.stderr,
            )
            skipped.append(path.as_posix())
            skipped_in[claimed[path]] += 1
            continue

        category = claimed[path]
        value = sizes[unit]
        files[category] += 1
        totals[category] += value
        if value > longest_value:
            longest_value, longest_path = value, path.as_posix()

        observations.append(
            {
                # The unit is in the key, not only in the attributes: a
                # project that switches from lines to words has changed
                # what the number means, and a series whose meaning
                # changes underneath its key is one nobody can read back.
                "key": f"{unit}:{path.as_posix()}",
                "value": float(value),
                "label": f"{path.as_posix()} [{category}]: {value} {unit}",
                # Only the lengths defined for this file: a binary
                # carries `bytes` alone, so a reader is never handed a
                # line count that was never counted.
                "attrs": {
                    "category": category,
                    **{name: str(size) for name, size in sizes.items()},
                },
            }
        )

    problem = nothing_measured(root, args.categories, len(claimed), len(observations))
    if problem:
        print(problem, file=sys.stderr)
        return 2

    # A category that matched nothing is named rather than omitted: it is
    # usually a glob with a typo in it, and an omitted category looks
    # exactly like a project that has no documentation.
    # A category whose files all failed to read is a third thing again,
    # and saying "nothing matched" about it names the wrong cause: the
    # glob is right and the files are unreadable.
    def describe(name: str) -> str:
        if files[name]:
            return f"{name} {files[name]} file(s), {totals[name]} {unit}"
        if skipped_in[name]:
            return f"{name} nothing readable ({skipped_in[name]} file(s) skipped)"
        return f"{name} nothing matched"

    breakdown = "; ".join(describe(name) for name in files)
    measured_files = len(observations)
    gaps = not_measured(skipped, unreadable_dirs(root))
    observations.append(gaps)
    emit(
        observations,
        summary=(
            f"{measured_files} file(s), {sum(totals.values())} {unit} "
            f"under {root.as_posix()} — {breakdown}; longest "
            f"{longest_path} ({longest_value} {unit}); {gaps['label']}"
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
