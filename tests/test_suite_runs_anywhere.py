"""The shipped suite tests the package, never the tree it happens to sit in.

`tests/` is synced to the public repo; `rules/`, `.vibe-sentinel.toml` and
this repository's own history are not. So a test that reaches outside the
shipped tree asks a question whose answer depends on which checkout it runs
in — and the two checkouts differ by exactly the files that make the
interesting answers true.

That is not hypothetical. `test_a_declared_question_is_not_pinned_here`
loaded the lenses of `Path.cwd()`, subtracted the shipped ones and asserted
the remainder was unpinned. Here that found eight declared lenses and looked
like a test. Synced to the public tree it found none, and the guard its
author had added to stop it being vacuous — `assert declared` — failed the
release at Phase 3. The guard was the tell: a test that must first check its
subject exists is a test that never constructed one.

Neither leak grep could have seen it. There was no textual reference from a
public file to a private one, only a behavioural dependency, and the only
instrument that catches those is running the suite where it will live. This
check moves that from release time to `pytest`.

What is refused here is the ambient directory — `Path.cwd()`, `os.getcwd()`
— which is wherever the process was started and never something a test built.

What is deliberately *not* refused is walking up from `__file__`, though it
reaches the same root. It was tried, and it flagged three tests that are
right: `test_layers.py` climbs to the root for `vibe_sentinel/`, and
`test_licenses.py` for `LICENSE` and the licence policy — the first two ship,
and the third does not and is already handled with `pytest.skip` and a
docstring saying why. Reaching the root is fine when what you reach is in the
manifest, and no parse can tell those apart. A rule that fails correct code
to catch a hypothetical is worse than the gap it closes.

The instrument that *can* tell them apart is behavioural: run the suite in a
tree where the files it must not depend on are absent. That is what caught
this bug, and it is a gate on every release rather than a thing anyone
remembers to do.

A test wanting a tree should build one under `tmp_path`, which is what every
other test here does, and what makes them mean the same thing everywhere. A
fact that really is about *this* repository belongs in `rules/tests/`.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent

#: Calls resolving to "wherever this process was started".
AMBIENT = {("Path", "cwd"), ("os", "getcwd")}


def _ambient_calls(tree: ast.AST) -> list[tuple[int, str]]:
    """Every `Path.cwd()` / `os.getcwd()` in one parsed module."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        owner = func.value
        if isinstance(owner, ast.Name) and (owner.id, func.attr) in AMBIENT:
            found.append((node.lineno, f"{owner.id}.{func.attr}()"))
    return found


def test_no_shipped_test_reads_the_ambient_directory() -> None:
    offenders: list[str] = []
    for path in sorted(TESTS.glob("*.py")):
        if path.name == Path(__file__).name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line, what in _ambient_calls(tree):
            offenders.append(f"{path.name}:{line}: {what}")

    assert not offenders, (
        "these tests read the directory they happen to run in, so they mean "
        "one thing here and another in the synced public tree:\n  "
        + "\n  ".join(sorted(offenders))
        + "\n\nBuild the tree the test needs under `tmp_path` instead. If the "
        "fact really is about *this* repository rather than about the "
        "package, it belongs in `rules/tests/`, which does not ship."
    )
