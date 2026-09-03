"""Probe: what error handlers do with the error, per directory.

Counts the handlers that discard it — ``except X: pass``,
``except X: return None``, ``with suppress(X):`` — against the number of
handlers each directory has at all.

This is not a lint pass. Whether any *single* swallow is wrong is a
question about that line, and other tools answer it (ruff's S110 and
S112, bandit's B110). What no per-line rule can see is the shape: a
package whose failures were all raised, logged or wrapped, and which now
discards a fifth of what it catches, has changed habit — and nothing in
any one diff says so, because each handler was added on its own and each
one looked local and reasonable. It is the commentary ratio's failure
mode transposed onto error handling, and it is the one an agent produces
by construction: the fastest way to make a traceback stop is to catch it.

A ``# noqa`` therefore does not move this number, because the number is
not a verdict. Nor is a rewrite: ``except X: pass`` and
``with suppress(X):`` are counted as the same fact, so migrating one to
the other — which ruff's SIM105 will do for you — reports no drift. The
same reasoning excludes taste from the classification entirely. A handler
is *silencing* when its body is drawn only from:

  - ``pass``
  - a bare constant expression — ``...``, or a string standing in for a
    comment
  - ``continue`` or ``break``
  - ``return`` with no value, a constant, or an empty literal collection
  - the whole of ``with suppress(...):``, which is this shape spelled
    explicitly

Anything else — a ``raise``, a log call, appending the exception
somewhere, a fallback that computes something — is a handler that does
something with the failure, whatever its quality. The set is enumerated
rather than inferred, so the number means the same thing between runs.

Which handlers catch *everything* (bare ``except:``, ``Exception``,
``BaseException``) is recorded alongside, not as a separate series: it is
the same event seen at its widest, and it is what a linter's default
already restricts itself to.

Usage::

    python -m vibe_sentinel.probes.handlers --root src --glob '*.py'
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

from vibe_sentinel.probes import (
    emit,
    iter_source_files,
    not_measured,
    nothing_measured,
    unreadable_dirs,
)

#: How many locations one directory's observation names. The rest are
#: counted, never dropped — the count is the measurement and the list is
#: for whoever has to go and look.
MAX_SITES = 10

#: Catching one of these, or nothing at all, catches everything.
BLANKET_NAMES = frozenset({"Exception", "BaseException"})


def dotted(node: ast.expr) -> str:
    """``a.b.C`` for an attribute chain, ``C`` for a name, else ``""``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def carries_nothing(value: ast.expr | None) -> bool:
    """True when a ``return`` hands the caller nothing about the failure.

    ``return None`` and ``return []`` are the swallow's other spelling:
    the caller cannot tell the failure from a legitimate empty answer.
    Empty literals only — ``return default`` computes something, and a
    non-empty literal is a decision about what the value should be.
    """
    if value is None or isinstance(value, ast.Constant):
        return True
    if isinstance(value, ast.List | ast.Tuple | ast.Set):
        return not value.elts
    return isinstance(value, ast.Dict) and not value.keys


def discards(node: ast.stmt) -> bool:
    """True when one statement does nothing with the exception."""
    if isinstance(node, ast.Pass | ast.Continue | ast.Break):
        return True
    if isinstance(node, ast.Expr):
        # `...`, or a string sitting where a comment would.
        return isinstance(node.value, ast.Constant)
    return isinstance(node, ast.Return) and carries_nothing(node.value)


def silencing_form(body: list[ast.stmt]) -> str | None:
    """Which discarding shape ``body`` is, or None if it does something.

    The form is reported so that a rising count can be read: handlers
    that return are a different habit from handlers that fall through,
    and the two arrive for different reasons.
    """
    if not body or not all(discards(node) for node in body):
        return None
    kinds = {type(node) for node in body}
    if ast.Return in kinds:
        return "return"
    if ast.Continue in kinds or ast.Break in kinds:
        return "loop-skip"
    return "pass"


def is_blanket(handler: ast.ExceptHandler) -> bool:
    """True when this handler catches everything that can be raised."""
    caught = handler.type
    if caught is None:
        return True
    parts = caught.elts if isinstance(caught, ast.Tuple) else [caught]
    return any(dotted(part).rsplit(".", 1)[-1] in BLANKET_NAMES for part in parts)


def suppressed(item: ast.expr) -> tuple[bool, bool] | None:
    """``(matched, blanket)`` if ``item`` is a ``suppress(...)`` call.

    Matched on the call's name rather than on a resolved import: this is
    ``contextlib.suppress``, ``suppress`` after a ``from`` import, and
    the alias someone gave it, all of which are the same construct. A
    different ``suppress`` would have to be a context manager named for
    what this one does.
    """
    if not isinstance(item, ast.Call):
        return None
    if dotted(item.func).rsplit(".", 1)[-1] != "suppress":
        return None
    return True, any(
        dotted(arg).rsplit(".", 1)[-1] in BLANKET_NAMES for arg in item.args
    )


class Tally:
    """Running totals for one directory."""

    __slots__ = ("blanket", "files", "forms", "handlers", "silent", "sites")

    def __init__(self) -> None:
        self.handlers = 0
        """Every ``except`` clause, plus every ``suppress`` block: the
        denominator. Three handlers on one ``try`` are three."""
        self.silent = 0
        self.blanket = 0
        self.forms: dict[str, int] = {}
        self.sites: list[tuple[str, int]] = []
        self.files: set[str] = set()

    def record(self, path: Path, lineno: int, form: str, blanket: bool) -> None:
        self.silent += 1
        self.blanket += int(blanket)
        self.forms[form] = self.forms.get(form, 0) + 1
        self.sites.append((path.as_posix(), lineno))
        self.files.add(path.as_posix())

    def where(self) -> str:
        """The locations, in source order, and how many are not named.

        Sorted rather than left in walk order: `ast.walk` is
        breadth-first, so the lines of one file come out interleaved by
        nesting depth, and the truncation below would then name ten
        arbitrary sites instead of the first ten.
        """
        sites = sorted(self.sites)
        named = ", ".join(f"{path}:{line}" for path, line in sites[:MAX_SITES])
        extra = len(sites) - MAX_SITES
        return named + (f", +{extra} more" if extra > 0 else "")


def measure_file(path: Path, tally: Tally) -> bool:
    """Fold one file's handlers into ``tally``; False if it was skipped.

    A file that cannot be read or parsed is noted on stderr — stdout
    carries the JSON protocol and must stay clean.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as e:
        print(f"skipped {path}: {e}", file=sys.stderr)
        return False

    # `ast.walk` finds handlers at any depth, and finds `except*` groups
    # without a version branch: both `Try` and `TryStar` hold their
    # clauses as ExceptHandler.
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            tally.handlers += 1
            form = silencing_form(node.body)
            if form is not None:
                tally.record(path, node.lineno, form, is_blanket(node))
        elif isinstance(node, ast.With | ast.AsyncWith):
            for item in node.items:
                match = suppressed(item.context_expr)
                if match is None:
                    continue
                tally.handlers += 1
                tally.record(path, item.context_expr.lineno, "suppress", match[1])
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vibe_sentinel.probes.handlers",
        description="Exception handlers that discard the error, per directory.",
    )
    parser.add_argument("--root", required=True, help="Directory to measure")
    parser.add_argument("--glob", default="*.py", help="File glob (default: *.py)")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"probe handlers: not a directory: {root}", file=sys.stderr)
        return 2

    per_dir: dict[str, Tally] = {}
    skipped: list[str] = []
    matched = parsed = 0
    for path in iter_source_files(root, args.glob):
        matched += 1
        tally = per_dir.setdefault(path.parent.as_posix(), Tally())
        if measure_file(path, tally):
            parsed += 1
        else:
            # `silent-total` is a sum over the files that parsed. A file
            # that did not is a hole in it, and a total that shrank
            # because of one otherwise reads as handlers being fixed.
            skipped.append(path.as_posix())

    problem = nothing_measured("handlers", root, args.glob, matched, parsed)
    if problem:
        print(problem, file=sys.stderr)
        return 2

    observations: list[dict] = []
    total_handlers = total_silent = total_blanket = 0
    for directory in sorted(per_dir):
        t = per_dir[directory]
        total_handlers += t.handlers
        total_silent += t.silent
        total_blanket += t.blanket
        if not t.silent:
            # No key for a directory that discards nothing, so the one
            # that starts to gets an `appeared` rather than a move from
            # zero. That a layer began doing this at all is the finding;
            # a threshold would be the wrong instrument for it.
            continue
        observations.append(
            {
                "key": f"silent-in:{directory}",
                "value": float(t.silent),
                "label": (
                    f"{directory}: {t.silent} of {t.handlers} handler(s) "
                    f"discard the error"
                ),
                "attrs": {
                    "handlers": str(t.handlers),
                    "share": f"{t.silent / t.handlers:.3f}",
                    "blanket": str(t.blanket),
                    "in_files": str(len(t.files)),
                    "forms": ", ".join(
                        f"{form} {count}" for form, count in sorted(t.forms.items())
                    ),
                    "where": t.where(),
                },
            }
        )

    # Tree-wide, and reported even at zero: the per-directory keys come
    # and go, so this is the series that is continuous enough to trend.
    observations.append(
        {
            "key": "silent-total",
            "value": float(total_silent),
            "label": (
                f"{total_silent} of {total_handlers} handler(s) tree-wide "
                f"discard the error"
            ),
            "attrs": {
                "handlers": str(total_handlers),
                "share": f"{total_silent / total_handlers:.3f}"
                if total_handlers
                else "0.000",
                "blanket": str(total_blanket),
                "directories": str(len(observations)),
            },
        }
    )

    # Counted before the gap observation is appended: this is how many
    # directories carry a `silent-in:` key, not how long the list is.
    reporting_dirs = len(observations) - 1
    gaps = not_measured(skipped, unreadable_dirs(root))
    observations.append(gaps)

    emit(
        observations,
        summary=(
            f"{total_silent} of {total_handlers} handler(s) discard the error, "
            f"across {reporting_dirs} of {len(per_dir)} director"
            f"{'y' if len(per_dir) == 1 else 'ies'}; {gaps['label']}"
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
