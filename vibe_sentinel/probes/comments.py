"""Probe: commentary vs code, per package.

Counts comment lines, comment characters, and code lines under a root,
grouped by package directory, and reports the ratio.

Why this is a structural question and not a style one: a model that
starts narrating every line moves a package's commentary ratio from
~0.15 to ~0.45 without any single comment being obviously wrong. No
per-line rule fires, but the shape of the file has changed. The ratio
catches it, and the drift comparison catches it moving.

Both lines and characters are reported because they fail differently: a
model can hold the line count flat while tripling comment length, or add
many short narrating comments. Either shifts one of the two numbers.

Usage::

    python -m vibe_sentinel.probes.comments --root src --glob '*.py'
"""

from __future__ import annotations

import argparse
import ast
import io
import sys
import tokenize
from pathlib import Path

from vibe_sentinel.probes import (
    emit,
    iter_source_files,
    not_measured,
    nothing_measured,
    unreadable_dirs,
)

#: Comment bodies that are instructions to other tools, not prose. A
#: linter directive is not a model padding its output, so counting one
#: as commentary would make a well-annotated package look over-commented.
#: Stored without the leading "#" so this list is not itself parsed as a
#: directive by tools that scan for one.
_DIRECTIVE_BODIES = (
    "!",  # shebang
    "-*-",  # encoding declaration
    "type:",
    "noqa",
    "pragma",
    "fmt:",
    "isort:",
    "ruff:",
    "mypy:",
    "nosec",
    "pylint:",
    "pyright:",
)


def is_directive(comment: str) -> bool:
    """True when a comment is a tool directive rather than commentary."""
    return comment.lstrip("#").strip().lower().startswith(_DIRECTIVE_BODIES)


class Counts:
    """Running totals for one package."""

    __slots__ = (
        "code_lines",
        "comment_chars",
        "comment_lines",
        "docstring_lines",
        "files",
    )

    def __init__(self) -> None:
        self.files = 0
        self.code_lines = 0
        self.comment_lines = 0
        self.comment_chars = 0
        self.docstring_lines = 0


def _docstring_lines(tree: ast.Module) -> set[int]:
    """Every line occupied by a docstring anywhere in the module.

    Docstrings are counted separately from `#` comments: they are usually
    policy-required, so folding them into the comment ratio would make a
    project that mandates docstrings look permanently over-commented.
    """
    covered: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            end = getattr(first, "end_lineno", None) or first.lineno
            covered.update(range(first.lineno, end + 1))
    return covered


def measure_file(path: Path, counts: Counts) -> bool:
    """Fold one file's counts into ``counts``. False if it could not be.

    A file that cannot be read or parsed is skipped with a note on
    stderr — stdout carries the JSON protocol and must stay clean. The
    caller counts the skips, because stderr is discarded on a probe that
    exits 0 and a ratio taken over an unknown subset of a package is not
    that package's ratio.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"skipped {path}: {e}", file=sys.stderr)
        return False
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as e:
        print(f"skipped {path}: {e}", file=sys.stderr)
        return False

    doc_lines = _docstring_lines(tree)
    comment_line_nos: set[int] = set()

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError) as e:
        print(f"skipped {path}: {e}", file=sys.stderr)
        return False

    for tok in tokens:
        if tok.type != tokenize.COMMENT:
            continue
        if is_directive(tok.string):
            continue
        comment_line_nos.add(tok.start[0])
        counts.comment_chars += len(tok.string.lstrip("#").strip())

    lines = text.splitlines()
    code = 0
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or i in doc_lines:
            continue
        # A trailing comment still sits on a code line; only a line whose
        # entire content is a comment stops being code.
        if stripped.startswith("#"):
            continue
        code += 1

    counts.files += 1
    counts.code_lines += code
    counts.comment_lines += len(comment_line_nos)
    counts.docstring_lines += len(doc_lines)
    return True


def package_of(path: Path, root: Path) -> str:
    """The package key a file is grouped under — its parent directory."""
    rel = path.relative_to(root).parent
    return (root / rel).as_posix() if rel != Path(".") else root.as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vibe_sentinel.probes.comments",
        description="Commentary vs code, per package.",
    )
    parser.add_argument("--root", required=True, help="Directory to measure")
    parser.add_argument("--glob", default="*.py", help="File glob (default: *.py)")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"probe comments: not a directory: {root}", file=sys.stderr)
        return 2

    per_package: dict[str, Counts] = {}
    skipped: list[str] = []
    matched = 0
    for path in iter_source_files(root, args.glob):
        matched += 1
        key = package_of(path, root)
        if not measure_file(path, per_package.setdefault(key, Counts())):
            skipped.append(path.as_posix())

    parsed = sum(c.files for c in per_package.values())
    problem = nothing_measured("comments", root, args.glob, matched, parsed)
    if problem:
        print(problem, file=sys.stderr)
        return 2

    observations = []
    total_code = total_comment = 0
    for key in sorted(per_package):
        c = per_package[key]
        total_code += c.code_lines
        total_comment += c.comment_lines
        if c.code_lines == 0:
            # A package with comments and no code is worth surfacing, but
            # a ratio over zero is not a number — report it as such.
            ratio = 0.0 if c.comment_lines == 0 else 1.0
        else:
            ratio = c.comment_lines / c.code_lines
        observations.append(
            {
                "key": key,
                "value": round(ratio, 4),
                "label": (
                    f"{key}: {c.comment_lines} comment lines / "
                    f"{c.code_lines} code lines across {c.files} file(s)"
                ),
                "attrs": {
                    "files": str(c.files),
                    "code_lines": str(c.code_lines),
                    "comment_lines": str(c.comment_lines),
                    "comment_chars": str(c.comment_chars),
                    "docstring_lines": str(c.docstring_lines),
                },
            }
        )

    gaps = not_measured(skipped, unreadable_dirs(root))
    observations.append(gaps)

    overall = (total_comment / total_code) if total_code else 0.0
    emit(
        observations,
        summary=(
            f"{len(per_package)} package(s); overall commentary ratio "
            f"{overall:.3f} ({total_comment} comment lines / {total_code} code "
            f"lines); {gaps['label']}"
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
