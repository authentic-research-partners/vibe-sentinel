"""Opt-in Python AST relationships over one explicit import-search root.

This measures source dependencies, not Python runtime equivalence or symbol
correctness. It never imports application modules or consults sys.path.
Existing modules/import-count observations keep their independent meaning.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from vibe_sentinel.probes import EXCLUDED_DIRS
from vibe_sentinel.relationships import edge_observation

MAX_FILES = 10000
MAX_FILE_BYTES = 2 * 1024 * 1024


class SourceModule(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    path: Path
    name: str
    package: bool = False
    namespace: bool = False
    tree: ast.Module | None = None
    bindings: set[str] = Field(default_factory=set)
    dynamic_attributes: bool = False


class Reader:
    def __init__(self, root: Path, source_root: Path) -> None:
        self.root = root.resolve()
        self.source_root = source_root
        self.modules: dict[str, list[SourceModule]] = {}
        self.files: list[SourceModule] = []
        self.gaps: set[tuple[str, str]] = set()
        self.observations: dict[str, dict[str, Any]] = {}

    def relative(self, path: Path) -> str:
        try:
            relative = path.relative_to(self.root)
            return (
                "outside-project"
                if ".." in relative.parts
                else relative.as_posix() or "."
            )
        except ValueError:
            return "outside-project"

    def gap(self, code: str, path: Path) -> None:
        self.gaps.add((code, self.relative(path)))

    def safe(self, path: Path) -> bool:
        try:
            parts = path.relative_to(self.root).parts
        except ValueError:
            return False
        if ".." in parts:
            return False
        current = self.root
        for part in parts:
            current /= part
            if current.is_symlink():
                return False
        return True

    def index(self) -> None:
        if (
            not self.root.is_dir()
            or not self.safe(self.source_root)
            or not self.source_root.is_dir()
        ):
            self.gap("invalid-source-root", self.source_root)
            return
        directories: list[Path] = []
        matched = 0

        def unreadable(error: OSError) -> None:
            self.gap("unreadable-directory", Path(error.filename or self.source_root))

        for current, subdirs, names in os.walk(
            self.source_root, followlinks=False, onerror=unreadable
        ):
            directory = Path(current)
            directories.append(directory)
            included = []
            for name in sorted(subdirs):
                if name in EXCLUDED_DIRS or name.endswith(".egg-info"):
                    continue
                if (directory / name).is_symlink():
                    self.gap("symlink-not-followed", directory / name)
                else:
                    included.append(name)
            subdirs[:] = included
            for filename in sorted(names):
                if not filename.endswith(".py"):
                    continue
                matched += 1
                path = directory / filename
                if matched > MAX_FILES:
                    self.gap("source-limit", self.source_root)
                    return
                if not self.safe(path):
                    self.gap("symlink-not-followed", path)
                    continue
                parts = list(path.relative_to(self.source_root).with_suffix("").parts)
                package = parts[-1] == "__init__"
                if package:
                    parts.pop()
                if not parts or not all(part.isidentifier() for part in parts):
                    self.gap("non-importable-source-path", path)
                    continue
                record = SourceModule(path=path, name=".".join(parts), package=package)
                self.modules.setdefault(record.name, []).append(record)
                try:
                    if not path.is_file():
                        self.gap("nonregular-source", path)
                        continue
                    if path.stat().st_size > MAX_FILE_BYTES:
                        self.gap("oversized-input", path)
                        continue
                    record.tree = ast.parse(
                        path.read_text(encoding="utf-8"), filename=self.relative(path)
                    )
                except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError):
                    self.gap("unparsed-source", path)
                    continue
                try:
                    record.bindings, record.dynamic_attributes = module_bindings(
                        record.tree
                    )
                except RecursionError:
                    self.gap("source-complexity", path)
                    continue
                self.files.append(record)
        if not matched:
            self.gap("no-python-source", self.source_root)
        for directory in directories:
            parts = list(directory.relative_to(self.source_root).parts)
            if not parts or not all(part.isidentifier() for part in parts):
                continue
            name = ".".join(parts)
            if not any(module.package for module in self.modules.get(name, [])):
                self.modules.setdefault(name, []).append(
                    SourceModule(
                        path=directory, name=name, package=True, namespace=True
                    )
                )
        for choices in self.modules.values():
            if len(choices) > 1:
                for choice in choices:
                    self.gap("ambiguous-module-path", choice.path)

    def edge(
        self,
        source: SourceModule,
        target: str,
        phase: str,
        resolution: str,
        kind: str = "import",
    ) -> None:
        observation = edge_observation(
            "pypi",
            f"file:{self.relative(source.path)}",
            target,
            kind,
            phase,
            resolution,
        )
        previous = self.observations.get(observation["key"])
        if previous:
            observation["value"] += previous["value"]
        self.observations[observation["key"]] = observation

    def target(
        self, name: str, source: SourceModule, *, relative: bool = False
    ) -> tuple[str, str] | None:
        choices = self.modules.get(name, [])
        if len(choices) > 1:
            self.gap("ambiguous-import", source.path)
            return None
        if choices:
            module = choices[0]
            if module.tree is None and not module.namespace:
                self.gap("unparsed-import-target", source.path)
                return None
            prefix = "namespace" if module.namespace else "file"
            return f"{prefix}:{self.relative(module.path)}", "local"
        local_top = name.split(".")[0] in self.modules
        if relative or local_top:
            self.gap("unresolved-internal-import", source.path)
            return f"unresolved:{name}", "unresolved"
        return f"package:{name}", "external"

    def from_import(
        self, node: ast.ImportFrom, source: SourceModule, phase: str
    ) -> None:
        package = (
            source.name.split(".") if source.package else source.name.split(".")[:-1]
        )
        if node.level:
            if node.level > len(package):
                self.gap("outside-relative-import", source.path)
                return
            base = ".".join(package[: len(package) - node.level + 1])
            if node.module:
                base += "." + node.module
        else:
            base = node.module or ""
        base_target = self.target(base, source, relative=bool(node.level))
        if base_target is None:
            return
        base_choices = self.modules.get(base, [])
        base_record = base_choices[0] if len(base_choices) == 1 else None
        resolved: set[tuple[str, str]] = set()
        for alias in node.names:
            if alias.name == "*":
                self.gap("wildcard-import", source.path)
                resolved.add(base_target)
                continue
            submodule = f"{base}.{alias.name}"
            if submodule in self.modules:
                if base_record and (
                    alias.name in base_record.bindings or base_record.dynamic_attributes
                ):
                    self.gap("ambiguous-from-import", source.path)
                    continue
                target = self.target(submodule, source, relative=True)
                if target:
                    resolved.add(target)
            else:
                # An attribute's existence is not a source-edge judgement.
                resolved.add(base_target)
        for target_name, resolution in sorted(resolved):
            self.edge(source, target_name, phase, resolution)

    def measure(self) -> dict[str, Any]:
        self.index()
        for module in self.files:
            assert module.tree is not None
            try:
                ImportVisitor(self, module).visit(module.tree)
            except RecursionError:
                self.gap("source-complexity", module.path)
        gaps = [{"code": code, "scope": scope} for code, scope in sorted(self.gaps)]
        observations = [self.observations[key] for key in sorted(self.observations)]
        observations.append(
            {
                "key": "scope:python-edges:1",
                "state": json.dumps(
                    {
                        "source_root": self.relative(self.source_root),
                        "definition": "python-ast-imports-v1",
                    },
                    sort_keys=True,
                ),
                "label": "Python source relationship scope",
            }
        )
        return {
            "observations": observations,
            "summary": f"{len(self.files)} Python sources; {len(gaps)} coverage gaps",
            "coverage": {"status": "incomplete" if gaps else "complete", "gaps": gaps},
        }


def module_bindings(tree: ast.Module) -> tuple[set[str], bool]:
    """Syntactic package attributes, excluding function/class-local bindings."""
    found: set[str] = set()
    dynamic = False

    def walk(node: ast.AST) -> None:
        nonlocal dynamic
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
            dynamic |= node.name == "__getattr__"
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            found.add(node.id)
            dynamic |= node.id == "__path__"
        elif isinstance(node, ast.Import):
            found.update(
                alias.asname or alias.name.split(".")[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            found.update(alias.asname or alias.name for alias in node.names)
            dynamic |= any(alias.name == "*" for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"exec", "globals", "setattr"}
        ):
            dynamic = True
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(tree)
    return found, dynamic


class ImportVisitor(ast.NodeVisitor):
    def __init__(self, reader: Reader, source: SourceModule) -> None:
        self.reader, self.source = reader, source
        self.phase = "runtime"
        self.bindings: Counter[str] = Counter()
        self.aliases: dict[str, tuple[str, int]] = {}
        self.candidates: dict[str, set[str]] = {}
        assert source.tree is not None
        for node in ast.walk(source.tree):
            if isinstance(node, ast.Name) and isinstance(
                node.ctx, (ast.Store, ast.Del)
            ):
                self.bindings[node.id] += 1
            elif isinstance(node, ast.arg):
                self.bindings[node.arg] += 1
            elif isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                self.bindings[node.name] += 1
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    self.bindings[alias.asname or alias.name.split(".")[0]] += 1
                    meaning = (
                        alias.name
                        if isinstance(node, ast.Import)
                        else f"{node.module}.{alias.name}"
                    )
                    self.candidates.setdefault(
                        alias.asname or alias.name.split(".")[0], set()
                    ).add(meaning)
            elif (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Store)
                and isinstance(node.value, ast.Name)
            ):
                self.bindings[node.value.id] += 1
            elif isinstance(node, ast.ExceptHandler) and node.name:
                self.bindings[node.name] += 1
            elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
                self.bindings[node.name] += 1
        for node in source.tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in {"typing", "importlib"}:
                        self.aliases[alias.asname or alias.name] = (
                            alias.name,
                            node.lineno,
                        )
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                for alias in node.names:
                    if (node.module, alias.name) in {
                        ("typing", "TYPE_CHECKING"),
                        ("importlib", "import_module"),
                    }:
                        self.aliases[alias.asname or alias.name] = (
                            f"{node.module}.{alias.name}",
                            node.lineno,
                        )

    def proved(self, name: str, meaning: str, line: int) -> bool:
        return (
            meaning.split(".")[0] not in self.reader.modules
            and self.bindings[name] == 1
            and name in self.aliases
            and self.aliases[name][0] == meaning
            and self.aliases[name][1] < line
        )

    def type_guard(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return self.proved(node.id, "typing.TYPE_CHECKING", node.lineno)
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "TYPE_CHECKING"
            and isinstance(node.value, ast.Name)
        ):
            return self.proved(node.value.id, "typing", node.lineno)
        return False

    def visit_If(self, node: ast.If) -> None:
        test = node.test
        inverted = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
        candidate = test.operand if isinstance(test, ast.UnaryOp) and inverted else test
        known = self.type_guard(candidate)
        if not known and any(
            (
                isinstance(item, ast.Name)
                and (
                    item.id == "TYPE_CHECKING"
                    or "typing.TYPE_CHECKING" in self.candidates.get(item.id, set())
                )
            )
            or isinstance(item, ast.Attribute)
            and item.attr == "TYPE_CHECKING"
            for item in ast.walk(test)
        ):
            self.reader.gap("ambiguous-type-guard", self.source.path)
        self.visit(test)
        previous = self.phase
        for branch, type_only in (
            (node.body, known and not inverted),
            (node.orelse, known and inverted),
        ):
            self.phase = "type" if previous == "type" or type_only else "runtime"
            for statement in branch:
                self.visit(statement)
        self.phase = previous

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            target = self.reader.target(alias.name, self.source)
            if target:
                self.reader.edge(self.source, target[0], self.phase, target[1])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.reader.from_import(node, self.source, self.phase)

    def visit_Call(self, node: ast.Call) -> None:
        name = node.func
        dynamic = False
        ambiguous = False
        if isinstance(name, ast.Name):
            if name.id == "__import__":
                dynamic = self.bindings[name.id] == 0
                ambiguous = not dynamic
            elif "importlib.import_module" in self.candidates.get(name.id, set()):
                dynamic = self.proved(name.id, "importlib.import_module", node.lineno)
                ambiguous = not dynamic
        elif (
            isinstance(name, ast.Attribute)
            and name.attr == "import_module"
            and isinstance(name.value, ast.Name)
        ):
            if name.value.id == "importlib" or "importlib" in self.candidates.get(
                name.value.id, set()
            ):
                dynamic = self.proved(name.value.id, "importlib", node.lineno)
                ambiguous = not dynamic
        if dynamic:
            self.reader.gap("dynamic-import", self.source.path)
            self.reader.edge(
                self.source,
                "dynamic:dynamic-import",
                self.phase,
                "dynamic",
                "dynamic-import",
            )
        elif ambiguous:
            self.reader.gap("ambiguous-dynamic-loader", self.source.path)
        self.generic_visit(node)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Static Python dependency relationships without importing application code."
    )
    parser.add_argument(
        "--root", required=True, help="Project root for stable file identities"
    )
    parser.add_argument(
        "--source-root",
        default=".",
        help="In-project import-search directory, relative to root (default: .)",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    source = root / args.source_root
    payload = Reader(root, source).measure()
    print(json.dumps(payload, ensure_ascii=False))
    if payload["coverage"]["status"] != "complete":
        sample = json.dumps(payload["coverage"]["gaps"][:2], ensure_ascii=True)
        print(
            f"Python relationship coverage incomplete: {sample[:280]}. Run python -I -m vibe_sentinel.probes.python_edges --root . --source-root <import-root> for full JSON coverage.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
