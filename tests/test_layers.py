"""The package's layers, declared — and the check that they hold.

One package, deliberately. What keeps a flat directory of forty modules
readable is not a subdirectory per concern; it is knowing, of any two
modules, which one is allowed to know about the other. That is a fact
about the code, so it is checked here rather than described somewhere and
believed.

Six layers, each named for what its modules *are*:

``foundation``
    Imports nothing else in the package. A constant, an exception, a unit
    of time, a log sink. Everything may depend on these and they depend on
    nobody, which is what makes them safe to import from the hook.
``vocabulary``
    The shapes and settings the rest of the package speaks in — the
    pydantic models that cross a boundary, the config, the probe
    template, what a gate policy is made of, how a database connection is
    opened.
``mechanisms``
    One job each, no opinions about the whole: diff two snapshots, run a
    subprocess, fit a series, post to a model, read or write a table.
``subjects``
    One question each, end to end: what the licences are, what the
    provenance is, what is sitting in the tree, what a command would do,
    what a change means. These are the modules a person goes to when they
    care about that question.
``composition``
    Puts subjects together and hands the result somewhere: the gate
    stage, the scan, the renderer, the tool-call hook.
``entry``
    Where a process starts — the CLI, ``__main__``, the probe scripts,
    and the lazy package ``__init__``.

A module may import from a lower layer or its own; never from a higher
one. Same-layer is allowed because a layer is not an ordering — ``engine``
using ``gates`` is two compositions cooperating, and pretending that is a
seventh layer would be arithmetic rather than structure.

**Only runtime imports count.** A name imported inside ``if
TYPE_CHECKING:`` costs no import, cannot make a cycle, and is how several
modules already refer upward for a type. Those are deliberate and stay
legal. Everything else counts, including an import deferred inside a
function: ``gates`` reaching for ``credentials`` in the body of a
collector is still ``gates`` depending on ``credentials``, and hiding
that from this check would make the check a description of the import
statements rather than of the package.

What this catches, and nothing else does: a new module that nobody placed,
a foundation module that quietly grew a dependency (and with it a cost the
hook pays on every tool call), an import cycle, and the ordinary slide
where a mechanism starts reaching up into the thing that composes it.
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "vibe_sentinel"

#: Every module in the package, by layer, lowest first. A module missing
#: from here fails the suite: placing it is a decision, and the point of
#: declaring the layers is that the decision gets made rather than
#: inherited from wherever the file happened to land.
LAYERS: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "foundation",
        frozenset(
            {
                "paths",
                "exceptions",
                "log",
                "horizons",
                "json_schema",
                "journal",
                "relationships",
                "node_targets",
                "rulesets",
                "probes",
                "db.schema",
            }
        ),
    ),
    (
        "vocabulary",
        frozenset(
            {
                "schemas",
                "pack_schemas",
                "packs",
                "config",
                "templates",
                "policy",
                "db.connection",
                "db.journal_store",
                "agents",
                "agents.claude_code",
                "agents.cursor",
                "agents.codex",
            }
        ),
    ),
    (
        "mechanisms",
        frozenset(
            {
                "inventory",
                "requirements",
                "runner",
                "trends",
                "llm",
                "python_environment",
                "security_pdf",
                "_python_inventory",
                "db.store",
                "db.gate_store",
                "db.migration",
            }
        ),
    ),
    (
        "subjects",
        frozenset(
            {
                "analyze",
                "backend",
                "licenses",
                "packages",
                "credentials",
                "safety",
                "security_artifacts",
                "security_osv",
                "security_setup",
                "security_source",
                "db.maintenance",
            }
        ),
    ),
    (
        "composition",
        frozenset(
            {
                "gates",
                "engine",
                "report",
                "results",
                "evidence_summary",
                "hook",
                "db",
                "protection",
                "security_checks",
            }
        ),
    ),
    (
        "entry",
        frozenset(
            {
                "",  # the package __init__ itself
                "cli",
                "__main__",
                "mcp_server",
                "probes.comments",
                "probes.dependencies",
                "probes.handlers",
                "probes.length",
                "probes.modules",
                "probes.python_edges",
                "probes.openapi",
                "probes.vite",
                "probes.artifact_support",
                "probes.alembic",
                "probes.json_schema",
                "probes.celery",
                "probes.patterns",
            }
        ),
    ),
)

#: Layer name -> its index, and module -> its layer index.
_RANK = {name: i for i, (name, _) in enumerate(LAYERS)}
_OF = {mod: i for i, (_, mods) in enumerate(LAYERS) for mod in mods}


def _module_name(path: Path) -> str:
    """``vibe_sentinel/db/store.py`` -> ``db.store``; a package -> its dir."""
    rel = path.relative_to(PACKAGE).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_type_checking(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _runtime_nodes(tree: ast.Module):
    """Every node except the bodies of ``if TYPE_CHECKING:`` blocks."""
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.If) and _is_type_checking(node):
            stack.extend(node.orelse)
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _graph() -> dict[str, set[str]]:
    """Module -> the modules it imports at runtime, by short name."""
    modules = {_module_name(p): p for p in sorted(PACKAGE.rglob("*.py"))}
    known = set(modules)
    edges: dict[str, set[str]] = defaultdict(set)

    for name, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _runtime_nodes(tree):
            targets: set[str] = set()
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "vibe_sentinel"
            ):
                base = (node.module or "").removeprefix("vibe_sentinel").lstrip(".")
                for alias in node.names:
                    # `from vibe_sentinel.db import store` names a module;
                    # `from vibe_sentinel.schemas import Change` names a class.
                    submodule = f"{base}.{alias.name}" if base else alias.name
                    targets.add(submodule if submodule in known else base)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("vibe_sentinel"):
                        targets.add(
                            alias.name.removeprefix("vibe_sentinel").lstrip(".")
                        )
            edges[name] |= {t for t in targets if t in known and t != name}

    return {name: edges[name] for name in modules}


def test_every_module_is_placed_in_a_layer() -> None:
    """A new module is a decision about where it sits, made here.

    Left out, it would still import whatever it liked and nothing would
    say so — which is how a flat package stops having a shape.
    """
    found = {_module_name(p) for p in PACKAGE.rglob("*.py")}
    undeclared = sorted(found - set(_OF))
    assert not undeclared, (
        f"not placed in a layer: {undeclared}. Add each to LAYERS in "
        f"tests/test_layers.py — lowest layer that can hold it."
    )

    stale = sorted(set(_OF) - found)
    assert not stale, (
        f"declared but no longer in the package: {stale}. A layer naming a "
        f"module nobody ships protects nothing."
    )


def test_no_module_imports_from_a_higher_layer() -> None:
    """The one rule. Lower or same; never up.

    An upward import is not a style complaint. It means the module that
    was supposed to be usable on its own now drags a composition in with
    it, and the first place that shows up is the hook's import budget.
    """
    violations: list[str] = []
    for module, imports in _graph().items():
        here = _OF[module]
        for target in sorted(imports):
            if _OF[target] > here:
                violations.append(
                    f"{module} ({LAYERS[here][0]}) imports "
                    f"{target} ({LAYERS[_OF[target]][0]})"
                )
    assert not violations, "upward imports:\n  " + "\n  ".join(violations)


def test_the_import_graph_is_acyclic() -> None:
    """Two modules that need each other are one module with a seam in it.

    Python tolerates some cycles and punishes others at import time,
    depending on which name is needed when. This says so before the
    tolerance runs out.
    """
    graph = _graph()
    state: dict[str, int] = {}
    cycles: list[str] = []

    def walk(node: str, path: list[str]) -> None:
        state[node] = 1
        for nxt in sorted(graph.get(node, ())):
            if state.get(nxt) == 1:
                cycles.append(" -> ".join([*path, node, nxt]))
            elif nxt not in state:
                walk(nxt, [*path, node])
        state[node] = 2

    for module in sorted(graph):
        if module not in state:
            walk(module, [])

    assert not cycles, "import cycles:\n  " + "\n  ".join(cycles)


def test_the_foundation_depends_on_nothing() -> None:
    """Stated separately because it is the layer with a cost attached.

    ``vibe-sentinel hook`` runs in front of every tool call, and what it
    is allowed to import is the whole reason ``paths.py`` holds three
    constants instead of living in ``config.py``. A foundation module
    growing one internal import is how that budget gets spent.
    """
    graph = _graph()
    for module in sorted(LAYERS[0][1]):
        assert not graph[module], (
            f"{module} is in the foundation layer but imports "
            f"{sorted(graph[module])}. Either it is not foundation, or that "
            f"import belongs somewhere else."
        )


@pytest.mark.parametrize("module", ["requirements", "node_targets"])
def test_install_target_readers_keep_the_hook_stdlib_boundary(module: str) -> None:
    """Composition moved requirements up a layer, not into the model stack."""
    tree = ast.parse((PACKAGE / f"{module}.py").read_text())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    assert all(
        name.split(".")[0] in sys.stdlib_module_names
        or (module == "requirements" and name == "vibe_sentinel.node_targets")
        for name in imports
    ), imports


@pytest.mark.parametrize("name", [name for name, _ in LAYERS])
def test_no_layer_is_empty(name: str) -> None:
    """A layer nobody is in is a layer nobody has to think about."""
    assert LAYERS[_RANK[name]][1]


#: What reads an ``ActionRequest`` and decides, and so may know no agent's
#: field names: the request and decision types, the journal, the gate.
ENGINE = frozenset({"journal", "db.journal_store", "safety", "requirements"})

#: Where an agent's adapter may be used: the hook that dispatches to one,
#: the commands that install one or report what one wired, and the
#: adapters themselves.
ADAPTER_USERS = frozenset({"hook", "cli", "protection", "report", "__main__"})


def _adapters(targets: set[str]) -> set[str]:
    return {t for t in targets if t == "agents" or t.startswith("agents.")}


def test_the_engine_knows_no_agent() -> None:
    """The boundary step 3 of the plan exists to make: a second agent costs
    an adapter file, never a branch in the gate. An engine module that
    imports an adapter has learned one agent's field names."""
    graph = _graph()
    leaks = {m: _adapters(graph[m]) for m in ENGINE if _adapters(graph[m])}
    assert not leaks, f"engine modules importing an agent adapter: {leaks}"


def test_only_the_rim_uses_an_adapter() -> None:
    graph = _graph()
    users = {
        m
        for m, targets in graph.items()
        if _adapters(targets) and not (m == "agents" or m.startswith("agents."))
    }
    assert users <= ADAPTER_USERS, (
        f"adapters used outside the rim: {sorted(users - ADAPTER_USERS)}. "
        f"Read the agent's payload in vibe_sentinel/agents/ and pass the "
        f"engine an ActionRequest."
    )
