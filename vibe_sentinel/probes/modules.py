"""Probe: where code lives, and how modules load each other.

Three structural questions in one pass:

  - **Shape.** How many modules each directory holds, and how many lines.
    A directory that was meant to hold three helpers and now holds
    nineteen is the classic agent drift — nothing was done wrong at any
    single step, and the layout is no longer the one anyone designed.
  - **Fan-out.** How many internal modules each module imports. A module
    whose fan-out doubles has quietly become a hub, whether or not that
    was intended.
  - **Fan-in.** How many internal modules import it. The other half of
    the same question, and the half that changes without the file being
    touched: a module gains importers because *other* files changed, so
    nothing in its own diff shows it becoming load-bearing.

Neither direction is a verdict. A dispatch layer with a fan-out of
sixteen is a dispatch layer doing its job, and a schema module everything
imports is what a schema module is for. What is worth knowing is the
movement, and which way it went — fan-out rising means this file reaches
further; fan-in rising means more of the codebase now depends on it, and
that is the one that makes a file expensive to change.

Both are reported per module, keyed by path, alongside a count per
directory, so the drift comparison surfaces a directory appearing for the
first time without needing a threshold — a new place where code started
landing is the finding.

Usage::

    python -m vibe_sentinel.probes.modules --root src
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

from vibe_sentinel.probes import emit, iter_source_files, nothing_measured


def module_name(path: Path, root: Path) -> str:
    """Dotted module name of ``path`` relative to ``root``'s parent."""
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join([root.name, *parts]) if parts else root.name


def _relative_base(importer: str, is_package: bool, level: int) -> str:
    """The package a relative import counts from.

    Level 1 means "this package": for ``pkg/sub/mod.py`` that is
    ``pkg.sub``, and for ``pkg/sub/__init__.py`` it is ``pkg.sub`` again,
    because a package's ``__init__`` *is* the package. Each further dot
    drops one more segment.
    """
    parts = importer.split(".")
    if not is_package:
        parts = parts[:-1]
    if level > 1:
        parts = parts[: -(level - 1)] or []
    return ".".join(parts)


def import_edges(
    tree: ast.Module, package: str, importer: str, is_package: bool
) -> list[tuple[str, tuple[str, ...]]]:
    """Every internal import ``importer`` makes, unresolved.

    Third-party and stdlib imports are excluded: fan-out onto the outside
    world is a dependency question, while fan-out onto your own modules
    is an organization question, and only the second is what this probe
    is about.

    Each entry is ``(base, names)`` — what the statement imports *from*,
    and what it names. ``from pkg.db import store`` cannot be resolved
    here: whether ``store`` is a module or an attribute of ``pkg.db``
    depends on the file map, which pass two has and this does not. So the
    ambiguity is handed on rather than guessed at.

    Relative imports are resolved to absolute names, which is not
    ambiguous and has to happen somewhere. An unresolved relative edge
    would be missing from the imported file's fan-in entirely, and a
    codebase that uses relative imports throughout would report every
    module as imported by nobody.
    """
    edges: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == package or alias.name.startswith(f"{package}."):
                    edges.append((alias.name, ()))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _relative_base(importer, is_package, node.level)
                base = f"{base}.{node.module}" if node.module else base
            elif node.module and (
                node.module == package or node.module.startswith(f"{package}.")
            ):
                base = node.module
            else:
                continue
            if base:
                edges.append((base, tuple(a.name for a in node.names)))
    return edges


def resolve(
    edges: list[tuple[str, tuple[str, ...]]], known: set[str], importer: str
) -> set[str]:
    """Turn one file's import statements into the modules it depends on.

    The most specific resolution wins. ``from pkg.db import store`` is a
    dependency on ``pkg/db/store.py``, not on ``pkg/db/__init__.py`` —
    the package is reached transitively, and transitive reach is not what
    fan-out measures. Where no name is a module, the statement is
    importing attributes and the dependency is on the base itself.

    Edges landing outside the measured tree are dropped. An import of
    something this scan never saw is a real import and not a fact about
    this tree's shape.
    """
    found: set[str] = set()
    for base, names in edges:
        specific = {f"{base}.{name}" for name in names} & known
        found |= specific or ({base} & known)
    return found - {importer}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vibe_sentinel.probes.modules",
        description="Directory shape and internal import coupling.",
    )
    parser.add_argument("--root", required=True, help="Package directory to inventory")
    parser.add_argument("--glob", default="*.py", help="File glob (default: *.py)")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"probe modules: not a directory: {root}", file=sys.stderr)
        return 2
    package = root.name

    dir_counts: dict[str, int] = {}
    dir_lines: dict[str, int] = {}
    parsed: list[tuple[Path, str, int, list[tuple[str, tuple[str, ...]]]]] = []
    by_module: dict[str, Path] = {}
    matched = 0

    # Pass one: parse, measure, and collect each file's import targets.
    # Fan-in cannot be counted here — a module's importers are spread
    # across files this loop has not reached yet.
    for path in iter_source_files(root, args.glob):
        matched += 1
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError) as e:
            print(f"skipped {path}: {e}", file=sys.stderr)
            continue

        name = module_name(path, root)
        directory = path.parent.as_posix()
        lines = len(text.splitlines())
        dir_counts[directory] = dir_counts.get(directory, 0) + 1
        dir_lines[directory] = dir_lines.get(directory, 0) + lines
        by_module[name] = path
        parsed.append(
            (
                path,
                name,
                lines,
                import_edges(tree, package, name, path.name == "__init__.py"),
            )
        )

    problem = nothing_measured("modules", root, args.glob, matched, len(parsed))
    if problem:
        print(problem, file=sys.stderr)
        return 2

    # Pass two: resolve each statement against the modules this scan
    # found, then invert the edges to get fan-in.
    known = set(by_module)
    importers: dict[str, set[str]] = {name: set() for name in by_module}
    resolved: dict[str, set[str]] = {}
    for _path, name, _lines, edges in parsed:
        targets = resolve(edges, known, name)
        resolved[name] = targets
        for target in targets:
            importers[target].add(name)

    observations: list[dict] = []
    for path, name, lines, _edges in parsed:
        targets = resolved[name]
        observations.append(
            {
                # `imports:`, not the `module:` this used to be. Resolving
                # `from pkg.db import store` to the submodule counts an edge
                # the old key never counted, so every recorded point of that
                # series means something the new one does not. A definition
                # change takes a new key — the old series ends where it was
                # last true, which is the honest shape for it.
                "key": f"imports:{path.as_posix()}",
                "value": float(len(targets)),
                "label": (
                    f"{path.as_posix()}: {len(targets)} internal import(s), "
                    f"{lines} lines"
                ),
                "attrs": {
                    "module": name,
                    "lines": str(lines),
                    "imports": ", ".join(sorted(targets)) or "(none)",
                },
            }
        )
        # A separate key, not a second value on the one above: an
        # observation carries one number, and folding fan-in into the
        # fan-out series would silently change what every recorded point
        # of it meant.
        inbound = importers[name]
        observations.append(
            {
                "key": f"imported-by:{path.as_posix()}",
                "value": float(len(inbound)),
                "label": (
                    f"{path.as_posix()}: imported by {len(inbound)} internal module(s)"
                ),
                "attrs": {
                    "module": name,
                    "importers": ", ".join(sorted(inbound)) or "(none)",
                },
            }
        )

    for directory in sorted(dir_counts):
        observations.append(
            {
                "key": f"dir:{directory}",
                "value": float(dir_counts[directory]),
                "label": (
                    f"{directory}: {dir_counts[directory]} module(s), "
                    f"{dir_lines[directory]} lines"
                ),
                "attrs": {"lines": str(dir_lines[directory])},
            }
        )

    edge_count = sum(len(e) for e in resolved.values())
    emit(
        observations,
        summary=(
            f"{len(parsed)} module(s) across {len(dir_counts)} director"
            f"{'y' if len(dir_counts) == 1 else 'ies'} under {root.as_posix()}, "
            f"{edge_count} internal import edge(s)"
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
