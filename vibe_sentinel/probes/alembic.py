"""Read literal Alembic revision declarations without importing migration code."""

from __future__ import annotations

import argparse
import ast
from collections import deque
import os
from pathlib import Path
from typing import Any

from vibe_sentinel.probes import EXCLUDED_DIRS
from vibe_sentinel.probes.artifact_support import (
    Evidence,
    contained,
    emit,
    read_bytes,
    relative,
    text,
)

MAX_ENTRIES = 20_000
MAX_FILES = 5_000
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_AST_NODES = 100_000
FIELDS = frozenset({"revision", "down_revision", "depends_on", "branch_labels"})


def names(value: Any) -> list[str]:
    if value is None:
        return []
    items = [value] if isinstance(value, str) else value
    if (
        not isinstance(items, (tuple, list))
        or not all(text(item) for item in items)
        or len(set(items)) != len(items)
    ):
        raise ValueError("unsupported revision identifiers")
    return sorted(items)


def declarations(data: bytes) -> dict[str, Any]:
    tree = ast.parse(data.decode("utf-8"))
    if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
        raise ValueError("AST limit")
    values: dict[str, Any] = {}
    for statement in tree.body:
        annotations = []
        if isinstance(statement, ast.AnnAssign):
            annotations.append(statement.annotation)
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = [
                *statement.args.posonlyargs,
                *statement.args.args,
                *statement.args.kwonlyargs,
            ]
            arguments += [
                arg
                for arg in (statement.args.vararg, statement.args.kwarg)
                if arg is not None
            ]
            annotations.extend(
                arg.annotation for arg in arguments if arg.annotation is not None
            )
            if statement.returns is not None:
                annotations.append(statement.returns)
        if any(
            isinstance(node, (ast.NamedExpr, ast.Call))
            for annotation in annotations
            for node in ast.walk(annotation)
        ):
            raise ValueError("computed annotation")
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            for alias in statement.names:
                if (
                    alias.asname or alias.name.split(".")[0]
                ) in FIELDS or alias.name == "*":
                    raise ValueError("metadata import binding")
            continue
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Function bodies are opaque. Decorators/default expressions execute
            # during import, so are outside this literal declaration projection.
            if (
                statement.name in FIELDS
                or statement.decorator_list
                or statement.args.defaults
                or any(value is not None for value in statement.args.kw_defaults)
            ):
                raise ValueError("unsupported function declaration")
            continue
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            name, value = statement.targets[0].id, statement.value
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.value is not None
        ):
            name, value = statement.target.id, statement.value
        else:
            raise ValueError("unsupported module statement")
        # Ignore no computed expressions, even under an unrelated variable:
        # their evaluation could also alter the revision namespace.
        literal = ast.literal_eval(value)
        if name not in FIELDS:
            continue
        if name in values:
            raise ValueError("metadata reassignment")
        values[name] = literal
    if (
        "revision" not in values
        or "down_revision" not in values
        or not text(values["revision"])
    ):
        raise ValueError("missing or invalid revision")
    return {
        "revision": values["revision"],
        "down_revision": names(values["down_revision"]),
        "depends_on": names(values.get("depends_on")),
        "branch_labels": names(values.get("branch_labels")),
    }


class Reader:
    def __init__(self, root: Path, versions: str) -> None:
        self.root = root.resolve()
        self.versions = versions
        self.evidence = Evidence("alembic")
        self.revisions: dict[str, dict[str, Any]] = {}
        self.entries = 0
        self.files = 0
        self.total_bytes = 0

    def walk(self) -> None:
        start = contained(self.root, self.versions, directory=True)
        pending = [start]
        while pending:
            directory = pending.pop()
            # Stream directory entries to enforce the bound before collecting
            # an arbitrarily large directory; share the core exclusion policy.
            with os.scandir(directory) as entries:
                for entry in entries:
                    self.entries += 1
                    if self.entries > MAX_ENTRIES:
                        self.evidence.gap("directory-entry-limit")
                        return
                    if entry.name in EXCLUDED_DIRS or entry.name.endswith(".egg-info"):
                        continue
                    path = Path(entry.path)
                    scope = path.relative_to(self.root).as_posix()
                    if entry.is_symlink():
                        self.evidence.gap("symlink-not-followed", scope)
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                    elif path.suffix == ".py":
                        self.files += 1
                        if self.files > MAX_FILES:
                            self.evidence.gap("migration-file-limit")
                            return
                        self.read(scope)
                        if self.total_bytes > MAX_TOTAL_BYTES:
                            return
        if not self.revisions:
            self.evidence.gap("no-revision-declarations")

    def read(self, path: str) -> None:
        try:
            data = read_bytes(self.root, path, MAX_SOURCE_BYTES)
            self.total_bytes += len(data)
            if self.total_bytes > MAX_TOTAL_BYTES:
                self.evidence.gap("total-source-byte-limit")
                return
            # An empty namespace marker is not a migration; a populated one
            # must satisfy the same declaration contract as every other file.
            if Path(path).name == "__init__.py" and not data.strip():
                return
            value = declarations(data)
            revision = value["revision"]
            if revision in self.revisions:
                self.evidence.gap("duplicate-revision-id")
                return
            self.revisions[revision] = {**value, "file": path}
        except (
            OSError,
            ValueError,
            UnicodeError,
            SyntaxError,
            RecursionError,
            TypeError,
        ):
            self.evidence.gap("unsupported-or-unreadable-migration", path)

    def topology(self) -> None:
        labels: dict[str, set[str]] = {}
        for revision, value in self.revisions.items():
            for label in value["branch_labels"]:
                labels.setdefault(label, set()).add(revision)
        if any(
            len(targets) != 1 or (label in self.revisions and targets != {label})
            for label, targets in labels.items()
        ):
            self.evidence.gap("ambiguous-branch-label")
        parents: dict[str, set[str]] = {revision: set() for revision in self.revisions}
        down_targets: set[str] = set()
        for revision, value in sorted(self.revisions.items()):
            self.evidence.record(
                "revision",
                revision,
                {"branch_labels": value["branch_labels"]},
                file=value["file"],
            )
            for kind in ("down_revision", "depends_on"):
                for name in value[kind]:
                    candidates = {name} if name in self.revisions else set()
                    if kind == "depends_on":
                        candidates |= labels.get(name, set())
                    if len(candidates) != 1:
                        self.evidence.gap(
                            "unknown-or-ambiguous-revision-target", revision
                        )
                        continue
                    target = next(iter(candidates))
                    parents[revision].add(target)
                    if kind == "down_revision":
                        down_targets.add(target)
                    self.evidence.edge(
                        f"revision:{revision}", f"revision:{target}", kind
                    )
        # Kahn's algorithm bounds the combined parent/dependency cycle check.
        dependents: dict[str, set[str]] = {revision: set() for revision in parents}
        remaining = {revision: len(targets) for revision, targets in parents.items()}
        for revision, targets in parents.items():
            for target in targets:
                dependents[target].add(revision)
        ready = deque(revision for revision, count in remaining.items() if count == 0)
        visited = 0
        while ready:
            revision = ready.popleft()
            visited += 1
            for child in dependents[revision]:
                remaining[child] -= 1
                if remaining[child] == 0:
                    ready.append(child)
        if visited != len(parents):
            self.evidence.gap("revision-cycle")
        for revision, value in sorted(self.revisions.items()):
            self.evidence.record(
                "position",
                revision,
                {
                    "base": not value["down_revision"],
                    "lineage_head": revision not in down_targets,
                },
            )

    def measure(self) -> dict[str, Any]:
        try:
            self.walk()
        except (OSError, ValueError):
            self.evidence.gap("unreadable-or-unsafe-version-directory")
        self.topology()
        self.evidence.record(
            "scope",
            "versions",
            {
                "definition": "alembic-literal-declarations-v1",
                "versions": self.versions if relative(self.versions) else "invalid",
                "runtime_metadata": "not-assessed",
                "deployed_database": "not-assessed",
                "migration_operations": "not-assessed",
            },
        )
        return self.evidence.result(
            "Literal Alembic revision topology; migration operations and database state not assessed"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inventory literal Alembic revision topology without importing migrations, running env.py or accessing a database."
    )
    parser.add_argument(
        "--root", required=True, help="Project root containing the migration directory"
    )
    parser.add_argument(
        "--versions",
        required=True,
        help="Migration versions directory relative to root",
    )
    args = parser.parse_args(argv)
    return emit(Reader(Path(args.root), args.versions).measure(), "Alembic")


if __name__ == "__main__":
    raise SystemExit(main())
