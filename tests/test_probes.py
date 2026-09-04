"""The shipped probe scripts, run as subprocesses.

They are commands, so they are tested as commands: run them, parse the
JSON they print, check the numbers. That also pins the probe protocol —
anything a company writes has to produce the same shape.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from vibe_sentinel.probes import NOT_MEASURED_KEY, unreadable_dirs
from vibe_sentinel.probes.patterns import resolve_ast_grep, run_ast_grep


def run_probe_module(module: str, args: list[str]) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", f"vibe_sentinel.probes.{module}", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "__init__.py").write_text('"""Package."""\n', encoding="utf-8")
    (pkg / "core.py").write_text(
        '"""Core."""\n'
        "\n"
        "# a real explanation of a non-obvious choice\n"
        "VALUE = 1\n"
        "\n"
        "def go():\n"
        "    return VALUE\n",
        encoding="utf-8",
    )
    (pkg / "sub" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "sub" / "helper.py").write_text(
        "from pkg import core\n\n\ndef helper():\n    return core.go()\n",
        encoding="utf-8",
    )
    return pkg


def test_comments_probe_reports_a_ratio_per_package(sample_tree: Path) -> None:
    payload = run_probe_module("comments", ["--root", str(sample_tree)])
    keys = {o["key"] for o in payload["observations"]}
    assert any(k.endswith("pkg") for k in keys)
    assert any(k.endswith("sub") for k in keys)
    assert "commentary ratio" in payload["summary"]


def test_comments_probe_ignores_tool_directives(tmp_path: Path) -> None:
    """`# noqa` is an instruction to another tool, not a model padding
    its output — counting it would make annotated code look verbose."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "m.py").write_text(
        "import os  # noqa: F401\n# type: ignore\nX = 1\n", encoding="utf-8"
    )
    payload = run_probe_module("comments", ["--root", str(pkg)])
    assert payload["observations"][0]["attrs"]["comment_lines"] == "0"


def test_comments_probe_counts_a_narrating_file_as_high_ratio(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "m.py").write_text(
        "# set x\nX = 1\n# set y\nY = 2\n# set z\nZ = 3\n", encoding="utf-8"
    )
    payload = run_probe_module("comments", ["--root", str(pkg)])
    assert payload["observations"][0]["value"] == pytest.approx(1.0)


def test_comments_probe_separates_docstrings_from_comments(tmp_path: Path) -> None:
    """Docstrings are usually policy-required; folding them into the
    comment ratio would make a docstring-mandating project look
    permanently over-commented."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "m.py").write_text(
        '"""Module docstring.\n\nSeveral\nlines\nlong.\n"""\n\nX = 1\n',
        encoding="utf-8",
    )
    obs = run_probe_module("comments", ["--root", str(pkg)])["observations"][0]
    assert obs["value"] == 0.0
    assert int(obs["attrs"]["docstring_lines"]) >= 5


def test_modules_probe_inventories_dirs_and_modules(sample_tree: Path) -> None:
    payload = run_probe_module("modules", ["--root", str(sample_tree)])
    keys = {o["key"] for o in payload["observations"]}
    assert any(k.startswith("dir:") for k in keys)
    assert any(k.startswith("imports:") for k in keys)
    assert any(k.startswith("imported-by:") for k in keys)


def test_modules_probe_counts_internal_imports_only(sample_tree: Path) -> None:
    """Fan-out onto your own modules is an organization question; fan-out
    onto the outside world is a dependency question."""
    payload = run_probe_module("modules", ["--root", str(sample_tree)])
    fan_out = {
        o["key"].removeprefix("imports:"): o["value"]
        for o in payload["observations"]
        if o["key"].startswith("imports:")
    }
    assert fan_out[f"{sample_tree.as_posix()}/sub/helper.py"] == 1.0
    assert fan_out[f"{sample_tree.as_posix()}/core.py"] == 0.0


def test_modules_probe_keys_are_stable_paths(sample_tree: Path) -> None:
    """Drift detection depends on the same thing landing under the same
    key between runs."""
    first = run_probe_module("modules", ["--root", str(sample_tree)])
    second = run_probe_module("modules", ["--root", str(sample_tree)])
    assert [o["key"] for o in first["observations"]] == [
        o["key"] for o in second["observations"]
    ]


def test_probe_rejects_a_root_that_is_not_a_directory(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "vibe_sentinel.probes.modules",
            "--root",
            str(tmp_path / "nope"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "not a directory" in result.stderr


def test_probe_skips_an_unparseable_file_without_failing(tmp_path: Path) -> None:
    """One broken file must not cost the whole scan."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "good.py").write_text("X = 1\n", encoding="utf-8")
    (pkg / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    payload = run_probe_module("modules", ["--root", str(pkg)])
    assert "1 module(s)" in payload["summary"]


def run_probe_raw(module: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a probe without asserting it succeeded."""
    return subprocess.run(
        [sys.executable, "-m", f"vibe_sentinel.probes.{module}", *args],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("module", ["modules", "comments", "handlers"])
def test_probe_fails_rather_than_reporting_zero_for_another_language(
    module: str, tmp_path: Path
) -> None:
    """Skipping every file is not a measurement of zero.

    Both probes read source through ``ast``, so a TypeScript tree is
    skipped file by file and would otherwise emit an empty observation
    list — identical to what a genuinely empty package emits, and read by
    the next comparison as "nothing here" rather than "wrong tool".
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.ts").write_text("export const x: number = 1;\n", encoding="utf-8")
    (src / "b.ts").write_text("interface Foo { bar: string }\n", encoding="utf-8")

    result = run_probe_raw(module, ["--root", str(src), "--glob", "*.ts"])

    assert result.returncode == 2
    assert result.stdout == ""
    assert "none of the 2 file(s)" in result.stderr
    # The remedy has to name the probe that would work here.
    assert "pattern-census" in result.stderr


@pytest.mark.parametrize("module", ["modules", "comments", "handlers"])
def test_probe_fails_when_the_glob_matches_nothing(module: str, tmp_path: Path) -> None:
    """No matches is a misconfiguration, not an empty package."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "notes.md").write_text("# notes\n", encoding="utf-8")

    result = run_probe_raw(module, ["--root", str(src), "--glob", "*.py"])

    assert result.returncode == 2
    assert "matched no files" in result.stderr
    assert "--root" in result.stderr and "--glob" in result.stderr


@pytest.mark.parametrize("module", ["modules", "comments", "handlers"])
def test_probe_still_succeeds_when_only_some_files_are_unparseable(
    module: str, tmp_path: Path
) -> None:
    """The guard fires on *total* failure only — one bad file is ordinary."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "good.py").write_text("X = 1\n", encoding="utf-8")
    (src / "bad.py").write_text("def broken(:\n", encoding="utf-8")

    result = run_probe_raw(module, ["--root", str(src)])

    assert result.returncode == 0
    assert json.loads(result.stdout)["observations"]


# --- what no probe measures ------------------------------------------------


def test_a_virtualenv_is_never_measured(tmp_path: Path) -> None:
    """The measurement that put this list here: pointed at `.`, the probes
    walked `.venv` and baselined 3,143 observations of other people's code
    against 74 of this project's. Worse than noise — every dependency
    upgrade then reads as drift in your own codebase."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
    vendored = tmp_path / ".venv" / "lib" / "python3.13" / "site-packages" / "dep"
    vendored.mkdir(parents=True)
    (vendored / "thing.py").write_text("y = 2\n", encoding="utf-8")

    payload = run_probe_module("modules", ["--root", str(tmp_path)])
    keys = {o["key"] for o in payload["observations"]}
    assert not any(".venv" in k or "site-packages" in k for k in keys)
    assert any("app" in k for k in keys)


@pytest.mark.parametrize(
    "excluded", ["node_modules", "__pycache__", "build", "dist", ".git", ".tox"]
)
def test_every_excluded_directory_is_skipped(tmp_path: Path, excluded: str) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
    noise = tmp_path / excluded / "pkg"
    noise.mkdir(parents=True)
    (noise / "thing.py").write_text("y = 2\n", encoding="utf-8")

    payload = run_probe_module("comments", ["--root", str(tmp_path)])
    assert not any(excluded in o["key"] for o in payload["observations"])


def test_a_root_inside_an_excluded_name_still_measures(tmp_path: Path) -> None:
    """Only what lies inside the measured tree counts. A project that
    genuinely lives under a directory called `build` is not excluded by
    its own prefix."""
    root = tmp_path / "build" / "myproject"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")

    payload = run_probe_module("comments", ["--root", str(root)])
    assert payload["observations"], "the root's own name must not exclude it"


# --- fan-in ----------------------------------------------------------------


def _pkg(root: Path, files: dict[str, str]) -> Path:
    """A package tree under `root/pkg`, written from {relpath: source}."""
    pkg = root / "pkg"
    for rel, src in files.items():
        path = pkg / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src, encoding="utf-8")
    return pkg


def _values(payload: dict, prefix: str) -> dict[str, float]:
    return {
        o["key"].removeprefix(prefix): o["value"]
        for o in payload["observations"]
        if o["key"].startswith(prefix)
    }


def test_fan_in_counts_the_modules_that_import_one(tmp_path: Path) -> None:
    """The half that changes without the file being touched: a module
    gains importers because *other* files changed."""
    pkg = _pkg(
        tmp_path,
        {
            "__init__.py": "",
            "core.py": "VALUE = 1\n",
            "a.py": "from pkg.core import VALUE\n",
            "b.py": "from pkg.core import VALUE\n",
            "c.py": "import pkg.core\n",
        },
    )
    payload = run_probe_module("modules", ["--root", str(pkg)])
    fan_in = _values(payload, "imported-by:")
    assert fan_in[f"{pkg.as_posix()}/core.py"] == 3
    # Nobody imports these, and that is recorded rather than omitted: a
    # module losing its last importer is a finding.
    assert fan_in[f"{pkg.as_posix()}/a.py"] == 0


def test_a_relative_import_is_resolved(tmp_path: Path) -> None:
    """Unresolved, it would be missing from the imported file's fan-in
    entirely — and a codebase using relative imports throughout would
    report every module as imported by nobody."""
    pkg = _pkg(
        tmp_path,
        {
            "__init__.py": "",
            "core.py": "VALUE = 1\n",
            "sub/__init__.py": "",
            "sub/leaf.py": "from ..core import VALUE\nfrom . import sibling\n",
            "sub/sibling.py": "X = 2\n",
        },
    )
    payload = run_probe_module("modules", ["--root", str(pkg)])
    fan_in = _values(payload, "imported-by:")
    assert fan_in[f"{pkg.as_posix()}/core.py"] == 1
    assert fan_in[f"{pkg.as_posix()}/sub/sibling.py"] == 1


def test_a_submodule_import_resolves_to_the_submodule(tmp_path: Path) -> None:
    """`from pkg.sub import mod` is a dependency on the module, not on the
    package's __init__ — the package is only reached transitively, and
    transitive reach is not what fan-out measures."""
    pkg = _pkg(
        tmp_path,
        {
            "__init__.py": "",
            "sub/__init__.py": "",
            "sub/mod.py": "X = 1\n",
            "user.py": "from pkg.sub import mod\n",
        },
    )
    payload = run_probe_module("modules", ["--root", str(pkg)])
    fan_in = _values(payload, "imported-by:")
    assert fan_in[f"{pkg.as_posix()}/sub/mod.py"] == 1
    assert fan_in[f"{pkg.as_posix()}/sub/__init__.py"] == 0


def test_importing_a_name_resolves_to_its_module(tmp_path: Path) -> None:
    """`from pkg.core import VALUE` names an attribute, so the dependency
    is on core itself."""
    pkg = _pkg(
        tmp_path,
        {
            "__init__.py": "",
            "core.py": "VALUE = 1\n",
            "user.py": "from pkg.core import VALUE\n",
        },
    )
    payload = run_probe_module("modules", ["--root", str(pkg)])
    assert _values(payload, "imported-by:")[f"{pkg.as_posix()}/core.py"] == 1


def test_an_import_of_something_outside_the_tree_is_not_an_edge(
    tmp_path: Path,
) -> None:
    """Stdlib and third-party are a dependency question, not a shape one."""
    pkg = _pkg(
        tmp_path,
        {"__init__.py": "", "user.py": "import json\nimport httpx\n"},
    )
    payload = run_probe_module("modules", ["--root", str(pkg)])
    assert _values(payload, "imports:")[f"{pkg.as_posix()}/user.py"] == 0


def test_importing_a_name_from_a_package_is_an_edge_to_the_package(
    tmp_path: Path,
) -> None:
    """`from pkg import THING` where THING is not a module imports an
    attribute of the package's __init__, and executing it is a real
    dependency on that file."""
    pkg = _pkg(
        tmp_path,
        {"__init__.py": "THING = 1\n", "user.py": "from pkg import THING\n"},
    )
    payload = run_probe_module("modules", ["--root", str(pkg)])
    assert _values(payload, "imported-by:")[f"{pkg.as_posix()}/__init__.py"] == 1


def test_fan_out_and_fan_in_are_separate_keys(tmp_path: Path) -> None:
    """One observation carries one number. Folding fan-in into the fan-out
    series would silently change what every recorded point of it meant."""
    pkg = _pkg(
        tmp_path,
        {
            "__init__.py": "",
            "core.py": "X = 1\n",
            "user.py": "from pkg.core import X\n",
        },
    )
    payload = run_probe_module("modules", ["--root", str(pkg)])
    keys = {o["key"] for o in payload["observations"]}
    assert f"imports:{pkg.as_posix()}/user.py" in keys
    assert f"imported-by:{pkg.as_posix()}/user.py" in keys
    # The old key carried the old definition and must not come back.
    assert not any(k.startswith("module:") for k in keys)


# --- what handlers do with the error ---------------------------------------


def _handlers(tmp_path: Path, source: str) -> dict:
    """Run the handlers probe over a one-file package holding `source`."""
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "m.py").write_text(source, encoding="utf-8")
    return run_probe_module("handlers", ["--root", str(src)])


def _total(payload: dict) -> dict:
    return next(o for o in payload["observations"] if o["key"] == "silent-total")


def _measurements(payload: dict) -> list[dict]:
    """The observations about the code, without the gap accounting.

    `not-measured` is emitted by every probe on every run, at zero when
    there is nothing to report, so a test counting what a probe found has
    to say which it means.
    """
    return [o for o in payload["observations"] if o["key"] != NOT_MEASURED_KEY]


def test_only_a_handler_that_does_nothing_counts(tmp_path: Path) -> None:
    """A handler that re-raises, wraps or records the failure is handling
    it, whatever its quality. Only a body that does nothing is a swallow,
    and the set of statements that count is enumerated rather than
    judged."""
    payload = _handlers(
        tmp_path,
        "def f(note):\n"
        "    try:\n"
        "        go()\n"
        "    except ValueError:\n"
        "        pass\n"
        "    try:\n"
        "        go()\n"
        "    except OSError:\n"
        "        raise\n"
        "    try:\n"
        "        go()\n"
        "    except KeyError as e:\n"
        "        note(e)\n",
    )
    total = _total(payload)
    assert total["value"] == 1.0
    assert total["attrs"]["handlers"] == "3"
    # When the count moves, the next question is where.
    where = _values(payload, "silent-in:")
    assert where[f"{tmp_path.as_posix()}/src"] == 1.0


def test_a_constant_return_is_a_discard(tmp_path: Path) -> None:
    """`return None` is the swallow's other spelling: the caller cannot
    tell the failure from a legitimate empty answer."""
    payload = _handlers(
        tmp_path,
        "def f():\n"
        "    try:\n"
        "        return go()\n"
        "    except ValueError:\n"
        "        return None\n"
        "    except KeyError:\n"
        "        return []\n",
    )
    assert _total(payload)["value"] == 2.0


def test_a_computed_fallback_is_not_a_discard(tmp_path: Path) -> None:
    """Returning something the handler worked out is a decision about
    what the value should be — a different thing from discarding."""
    payload = _handlers(
        tmp_path,
        "def f(default):\n"
        "    try:\n"
        "        return go()\n"
        "    except ValueError:\n"
        "        return default\n"
        "    except KeyError:\n"
        "        return recover()\n",
    )
    assert _total(payload)["value"] == 0.0


def test_suppress_counts_the_same_as_except_pass(tmp_path: Path) -> None:
    """ruff's SIM105 rewrites the first into the second. Counting only
    the try/except form would report that rewrite as errors having
    stopped being swallowed — a measurement of style, not of structure."""
    written = _handlers(
        tmp_path / "a",
        "def f():\n    try:\n        go()\n    except OSError:\n        pass\n",
    )
    rewritten = _handlers(
        tmp_path / "b",
        "from contextlib import suppress\n"
        "\n"
        "def f():\n"
        "    with suppress(OSError):\n"
        "        go()\n",
    )
    assert _total(written)["value"] == _total(rewritten)["value"] == 1.0
    # Both the numerator and the denominator, or the share would move.
    assert _total(written)["attrs"]["handlers"] == "1"
    assert _total(rewritten)["attrs"]["handlers"] == "1"


def test_a_directory_that_discards_nothing_gets_no_key(tmp_path: Path) -> None:
    """So the layer that starts discarding errors reports an `appeared`
    rather than a move from zero: that it began at all is the finding.
    The tree-wide total is reported anyway — it is the series that has to
    stay continuous to be worth a trend."""
    payload = _handlers(
        tmp_path,
        "def f():\n    try:\n        go()\n    except OSError:\n        raise\n",
    )
    assert [o["key"] for o in _measurements(payload)] == ["silent-total"]
    assert _total(payload)["value"] == 0.0


def test_a_blanket_catch_is_counted_and_marked(tmp_path: Path) -> None:
    """The same event at its widest. Recorded beside the count rather
    than as its own series: a linter's default restricts itself to these,
    which is exactly the restriction this probe does not make."""
    payload = _handlers(
        tmp_path,
        "def f():\n"
        "    try:\n"
        "        go()\n"
        "    except:\n"
        "        pass\n"
        "    try:\n"
        "        go()\n"
        "    except Exception:\n"
        "        pass\n"
        "    try:\n"
        "        go()\n"
        "    except ValueError:\n"
        "        pass\n",
    )
    total = _total(payload)
    assert total["value"] == 3.0
    assert total["attrs"]["blanket"] == "2"


def test_an_except_star_clause_is_a_handler(tmp_path: Path) -> None:
    """`except*` holds its clauses as the same node, so the walk finds
    them without a version branch — and a group that discards discards."""
    payload = _handlers(
        tmp_path,
        "def f():\n    try:\n        go()\n    except* ValueError:\n        pass\n",
    )
    assert _total(payload)["value"] == 1.0


# --- how long files are ----------------------------------------------------


def _tree(root: Path, files: dict[str, str]) -> Path:
    """A tree written from {relpath: content}."""
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _length(root: Path, categories: str, *rest: str) -> dict:
    return run_probe_module(
        "length", ["--root", str(root), "--categories", categories, *rest]
    )


def test_length_reports_every_file_under_the_category_that_claimed_it(
    tmp_path: Path,
) -> None:
    root = _tree(
        tmp_path,
        {
            "CLAUDE.md": "# rules\n" * 10,
            "docs/guide.md": "text\n" * 4,
            "src/app.py": "x = 1\n" * 7,
        },
    )
    payload = _length(root, "agent=CLAUDE.md; docs=*.md; code=*.py")

    values = _values(payload, "lines:")
    assert values[f"{root.as_posix()}/CLAUDE.md"] == 10.0
    assert values[f"{root.as_posix()}/docs/guide.md"] == 4.0
    assert values[f"{root.as_posix()}/src/app.py"] == 7.0

    by_key = {o["key"]: o for o in payload["observations"]}
    assert by_key[f"lines:{root.as_posix()}/CLAUDE.md"]["attrs"]["category"] == "agent"
    assert by_key[f"lines:{root.as_posix()}/docs/guide.md"]["attrs"]["category"] == (
        "docs"
    )


def test_the_first_matching_category_claims_the_file(tmp_path: Path) -> None:
    """Entry order is the semantics, not a formality. With `docs=*.md`
    first, CLAUDE.md is documentation and the category meant to watch it
    is empty — the mistake the order exists to let a project avoid."""
    root = _tree(tmp_path, {"CLAUDE.md": "a\n"})

    specific = _length(root, "agent=CLAUDE.md; docs=*.md")
    general = _length(root, "docs=*.md; agent=CLAUDE.md")

    assert len(_measurements(specific)) == 1, "one file, counted once"
    assert _measurements(specific)[0]["attrs"]["category"] == "agent"
    assert _measurements(general)[0]["attrs"]["category"] == "docs"
    assert "agent nothing matched" in general["summary"]


def test_moving_a_file_between_categories_does_not_re_key_it(tmp_path: Path) -> None:
    """The category labels an observation; it is not part of its key.
    Otherwise editing CATEGORIES would report every file as disappeared
    and an identical one as appeared — a reorganisation that never
    happened."""
    root = _tree(tmp_path, {"CLAUDE.md": "a\n"})

    as_docs = _length(root, "docs=*.md")
    as_agent = _length(root, "agent=CLAUDE.md")

    assert {o["key"] for o in as_docs["observations"]} == {
        o["key"] for o in as_agent["observations"]
    }


def test_the_unit_is_part_of_the_key(tmp_path: Path) -> None:
    """A project switching to words has changed what the number means.
    The old series ends where it was last true rather than being
    redefined under its own key."""
    root = _tree(tmp_path, {"docs/a.md": "one two three\nfour five\n"})

    lines = _length(root, "docs=*.md")
    words = _length(root, "docs=*.md", "--unit", "words")

    assert _values(lines, "lines:") == {f"{root.as_posix()}/docs/a.md": 2.0}
    assert _values(words, "words:") == {f"{root.as_posix()}/docs/a.md": 5.0}
    assert not _values(words, "lines:")


def test_rewrapping_prose_moves_lines_and_leaves_words_alone(tmp_path: Path) -> None:
    """Why the unit is a parameter at all: rewrapping a paragraph halves
    the line count of a file whose content did not change."""
    root = _tree(tmp_path, {"docs/a.md": "one two three four five six seven eight\n"})
    before_lines = _length(root, "docs=*.md")
    before_words = _length(root, "docs=*.md", "--unit", "words")

    (root / "docs" / "a.md").write_text(
        "one two\nthree four\nfive six\nseven eight\n", encoding="utf-8"
    )
    after_lines = _length(root, "docs=*.md")
    after_words = _length(root, "docs=*.md", "--unit", "words")

    assert _values(before_lines, "lines:") != _values(after_lines, "lines:")
    assert _values(before_words, "words:") == _values(after_words, "words:")


def test_every_length_is_recorded_whatever_the_unit_is(tmp_path: Path) -> None:
    """Picking one unit loses nothing: "412 lines, 3,100 words" is what
    answers "is this prose or is this code" without a second run."""
    text = "one two three\nfour five\n"
    root = _tree(tmp_path, {"docs/a.md": text})

    payload = _length(root, "docs=*.md", "--unit", "bytes")

    assert payload["observations"][0]["attrs"] == {
        "category": "docs",
        "lines": "2",
        "words": "5",
        "bytes": str(len(text.encode("utf-8"))),
    }


def test_a_category_that_matched_nothing_is_named_rather_than_omitted(
    tmp_path: Path,
) -> None:
    """A glob with a typo in it must read as a typo. An omitted category
    looks exactly like a project that has no documentation."""
    root = _tree(tmp_path, {"src/app.py": "x = 1\n"})

    payload = _length(root, "code=*.py; docs=*.mkd")

    assert "docs nothing matched" in payload["summary"]
    assert "code 1 file(s)" in payload["summary"]


def test_matching_nothing_at_all_is_an_error_not_a_measurement_of_zero(
    tmp_path: Path,
) -> None:
    """The rule the other probes keep: an empty observation list is what a
    correctly configured probe emits for a tree holding none of these
    files, so a typo must not be recorded as a finding about the tree."""
    root = _tree(tmp_path, {"src/app.py": "x = 1\n"})

    result = run_probe_raw("length", ["--root", str(root), "--categories", "docs=*.md"])

    assert result.returncode == 2
    assert result.stdout == ""
    assert "matched any category" in result.stderr
    assert "--root" in result.stderr and "--categories" in result.stderr


def test_a_malformed_category_names_the_form_it_wanted(tmp_path: Path) -> None:
    """Dropped rather than refused, it would report as a category that
    matched nothing — which is a fact about the tree, and this is not."""
    result = run_probe_raw(
        "length", ["--root", str(tmp_path), "--categories", "docs *.md"]
    )

    assert result.returncode == 2
    assert "name=glob" in result.stderr


def test_a_file_that_is_not_text_is_skipped(tmp_path: Path) -> None:
    """This probe measures the length of text; stdout carries the
    protocol, so the note goes to stderr."""
    root = _tree(tmp_path, {"docs/a.md": "text\n"})
    (root / "docs" / "logo.bin").write_bytes(b"\x00\xff\xfe")

    result = run_probe_raw(
        "length", ["--root", str(root), "--categories", "docs=*.md,*.bin"]
    )

    assert result.returncode == 0
    assert set(_values(json.loads(result.stdout), "lines:")) == {
        f"{root.as_posix()}/docs/a.md"
    }
    assert "not UTF-8 text" in result.stderr


def test_a_tree_of_nothing_but_binaries_is_the_never_taken_measurement(
    tmp_path: Path,
) -> None:
    root = _tree(tmp_path, {})
    (root / "logo.bin").write_bytes(b"\x00\xff\xfe")

    result = run_probe_raw(
        "length", ["--root", str(root), "--categories", "blobs=*.bin"]
    )

    assert result.returncode == 2
    assert "UTF-8 text" in result.stderr


def test_a_binary_is_measured_in_bytes(tmp_path: Path) -> None:
    """A PNG has a size. That is the whole of what is defined for it, and
    it is what a project watching the weight of what it ships wants."""
    root = _tree(tmp_path, {})
    blob = b"\x89PNG\r\n" + b"\x00" * 500
    (root / "logo.png").write_bytes(blob)

    payload = _length(root, "images=*.png", "--unit", "bytes")

    assert _values(payload, "bytes:") == {
        f"{root.as_posix()}/logo.png": float(len(blob))
    }


def test_a_binary_carries_no_line_or_word_count(tmp_path: Path) -> None:
    """Counting either would be inventing a number, and an attribute is
    read as a measurement that was taken."""
    root = _tree(tmp_path, {"notes.md": "one two\n"})
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\xff")

    payload = _length(root, "images=*.png; docs=*.md", "--unit", "bytes")
    attrs = {
        Path(o["key"].removeprefix("bytes:")).name: o["attrs"]
        for o in payload["observations"]
        if o["key"].startswith("bytes:")
    }

    assert set(attrs["logo.png"]) == {"category", "bytes"}
    assert set(attrs["notes.md"]) == {"category", "lines", "words", "bytes"}


@pytest.mark.parametrize("unit", ["lines", "words"])
def test_a_binary_is_still_skipped_under_any_other_unit(
    tmp_path: Path, unit: str
) -> None:
    """`bytes` is the exception, not a general widening."""
    root = _tree(tmp_path, {"notes.md": "one two\n"})
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\xff")

    result = run_probe_raw(
        "length",
        [
            "--root",
            str(root),
            "--categories",
            "images=*.png; docs=*.md",
            "--unit",
            unit,
        ],
    )

    assert result.returncode == 0
    assert _values(json.loads(result.stdout), f"{unit}:").keys() == {
        f"{root.as_posix()}/notes.md"
    }
    assert "--unit bytes" in result.stderr, "the skip note names the remedy"


def test_length_never_measures_a_virtualenv(tmp_path: Path) -> None:
    """Load-bearing here in a way it is not for the other probes: this is
    the one whose root is the project root by default, which is exactly
    where a virtualenv sits."""
    root = _tree(tmp_path, {"README.md": "docs\n"})
    vendored = root / ".venv" / "lib" / "python3.13" / "site-packages" / "dep"
    vendored.mkdir(parents=True)
    (vendored / "README.md").write_text("someone else's\n", encoding="utf-8")

    payload = _length(root, "docs=*.md")

    assert set(_values(payload, "lines:")) == {f"{root.as_posix()}/README.md"}


# ---------------------------------------------------------------------------
# Which binary pattern-census actually runs
# ---------------------------------------------------------------------------


def _fake_binary(directory: Path, name: str, script: str) -> Path:
    binary = directory / name
    binary.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


def test_an_sg_that_is_not_ast_grep_is_refused_rather_than_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sg` is ast-grep's short name and shadow-utils' setgid tool, which
    ships at /usr/bin/sg on most Linux distributions. Run by mistake it
    exits 1 with no stdout — indistinguishable here from ast-grep finding
    nothing — so taking it on the strength of its name records a confident
    zero for a measurement that never ran, and the real count arrives as
    drift the first time the right binary is on PATH."""
    _fake_binary(tmp_path, "sg", "echo 'Usage: sg group [[-c] command]' >&2\nexit 1")
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(RuntimeError, match="ast-grep not found"):
        resolve_ast_grep()


def test_sg_is_taken_when_it_says_it_is_ast_grep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The short name is still ast-grep's own, so it stays usable — on the
    binary's word rather than its filename."""
    binary = _fake_binary(tmp_path, "sg", "echo 'ast-grep 0.45.3'")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert resolve_ast_grep() == str(binary)


def test_a_pattern_scan_that_could_not_read_the_tree_is_not_a_count(
    tmp_path: Path,
) -> None:
    """ast-grep reports an unreadable path on stderr and still exits 0 with
    JSON for the rest, so the count comes back short and confident. The
    missing matches would then arrive as drift the day the permission is
    fixed — the same fabricated movement the binary-identity check exists
    to prevent."""
    readable = tmp_path / "ok"
    readable.mkdir()
    (readable / "a.py").write_text("import subprocess\nsubprocess.run(['ls'])\n")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "b.py").write_text("import subprocess\nsubprocess.run(['ls'])\n")
    locked.chmod(0o000)
    try:
        with pytest.raises(RuntimeError, match="could not read part of"):
            run_ast_grep("subprocess.run($$$A)", "python", tmp_path, 30.0)
    finally:
        locked.chmod(0o755)


# ---------------------------------------------------------------------------
# What a probe could not measure
# ---------------------------------------------------------------------------


def test_every_probe_accounts_for_what_it_could_not_measure(tmp_path: Path) -> None:
    """The key is emitted on every run, at zero when nothing was missed.

    Always present makes it a series rather than a note: these probes
    carry `tolerance = 0`, so the run where it leaves zero is the run it
    is reported on."""
    pkg = _pkg(
        tmp_path,
        {
            "__init__.py": "",
            "a.py": "import os\ntry:\n    x = 1\nexcept OSError:\n    pass\n",
        },
    )
    payloads = {
        "comments": run_probe_module("comments", ["--root", str(pkg)]),
        "modules": run_probe_module("modules", ["--root", str(pkg)]),
        "handlers": run_probe_module("handlers", ["--root", str(pkg)]),
        "length": run_probe_module(
            "length", ["--root", str(pkg), "--categories", "code=*.py"]
        ),
    }
    for name, payload in payloads.items():
        gap = next(
            (o for o in payload["observations"] if o["key"] == NOT_MEASURED_KEY), None
        )
        assert gap is not None, f"{name} reported no gap accounting"
        assert gap["value"] == 0.0, f"{name} claimed a gap in a readable tree"
        assert gap["label"] in payload["summary"], f"{name} kept it out of the summary"


@pytest.mark.parametrize("probe", ["comments", "modules", "handlers"])
def test_a_file_that_cannot_be_parsed_is_counted_not_dropped(
    tmp_path: Path, probe: str
) -> None:
    """A skipped file used to reach stderr only, and stderr is discarded on
    a probe that exits 0. So the aggregates came back smaller and perfectly
    confident: one unparseable module lowers the fan-in of everything it
    imports, and the drop is reported as drift in files nobody touched."""
    pkg = _pkg(
        tmp_path,
        {"__init__.py": "", "good.py": "import os\n", "broken.py": "def f(:\n"},
    )
    payload = run_probe_module(probe, ["--root", str(pkg)])
    gap = next(o for o in payload["observations"] if o["key"] == NOT_MEASURED_KEY)
    assert gap["value"] == 1.0
    assert "broken.py" in gap["label"]
    assert gap["attrs"]["unparsed"] == "1"


def test_a_directory_that_cannot_be_read_is_counted_not_absent(tmp_path: Path) -> None:
    """`Path.rglob` swallows a PermissionError and keeps going, so an
    unreadable subtree is indistinguishable from one that is not there —
    and the day the permission is fixed its files arrive as drift."""
    pkg = _pkg(tmp_path, {"__init__.py": "", "a.py": "import os\n"})
    locked = pkg / "locked"
    locked.mkdir()
    (locked / "b.py").write_text("import os\n", encoding="utf-8")
    locked.chmod(0o000)
    try:
        assert unreadable_dirs(pkg) == ["locked"]
        payload = run_probe_module("comments", ["--root", str(pkg)])
        gap = next(o for o in payload["observations"] if o["key"] == NOT_MEASURED_KEY)
        assert gap["value"] == 1.0
        assert gap["attrs"]["unreadable_dirs"] == "1"
    finally:
        locked.chmod(0o755)


def test_an_unreadable_dependency_directory_is_not_this_projects_problem(
    tmp_path: Path,
) -> None:
    """EXCLUDED_DIRS is pruned before descent, so a virtualenv nobody can
    read is not a finding about the code that was measured."""
    pkg = _pkg(tmp_path, {"__init__.py": "", "a.py": "import os\n"})
    venv = pkg / ".venv"
    venv.mkdir()
    venv.chmod(0o000)
    try:
        assert unreadable_dirs(pkg) == []
    finally:
        venv.chmod(0o755)


def test_pattern_total_counts_only_what_the_breakdown_counts(tmp_path: Path) -> None:
    """ast-grep is handed the whole root and finds the virtualenv too. The
    per-directory keys were filtered and the total was not, so the total
    counted the dependencies — and every upgrade of them would read as
    drift in this codebase."""
    root = tmp_path / "proj"
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("import subprocess\nsubprocess.run(['ls'])\n")
    (root / ".venv" / "lib" / "dep.py").write_text(
        "import subprocess\nsubprocess.run(['ls'])\nsubprocess.run(['pwd'])\n"
    )
    payload = run_probe_module(
        "patterns",
        ["--root", str(root), "--pattern", "subprocess.run($$$A)", "--lang", "python"],
    )
    total = next(o for o in payload["observations"] if o["key"] == "pattern-total")
    per_dir = sum(
        o["value"]
        for o in payload["observations"]
        if o["key"].startswith("pattern-in:")
    )
    assert total["value"] == per_dir == 1.0
    assert "1 match(es)" in payload["summary"]


def test_a_category_whose_files_were_all_unreadable_says_so(tmp_path: Path) -> None:
    """ "nothing matched" names the wrong cause: the glob is right and the
    files are unreadable, which is a different thing to go and fix."""
    root = _tree(tmp_path, {"a.md": "text\n"})
    (root / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    payload = _length(root, "docs=*.md; binaries=*.bin")
    assert "binaries nothing readable (1 file(s) skipped)" in payload["summary"]
    assert "binaries nothing matched" not in payload["summary"]


# ---------------------------------------------------------------------------
# dependencies — the environment, which is the part of a project nothing
# else records
# ---------------------------------------------------------------------------


def test_dependencies_probe_records_a_version_and_an_origin_per_package() -> None:
    """Two series, not one observation carrying two facts. Folding them
    would silently change what every recorded point of the series meant."""
    payload = run_probe_module("dependencies", ["--scope", "all"])
    keys = {o["key"] for o in payload["observations"]}
    # pydantic is a runtime dependency of this package, so it is installed
    # in any environment these tests run in.
    assert "version:pydantic" in keys
    assert "origin:pydantic" in keys


def test_dependencies_probe_puts_the_version_in_state_not_value() -> None:
    """A version is an identity, not a magnitude — 1.10 does not sort
    after 1.9 — so it must not reach the column trends are fitted over."""
    payload = run_probe_module("dependencies", ["--scope", "all"])
    version = next(o for o in payload["observations"] if o["key"] == "version:pydantic")
    assert version.get("value") is None
    assert version["state"].count(".") >= 1


def test_dependencies_probe_keys_survive_an_upgrade() -> None:
    """The whole point. `version:pydantic` is the key whether pydantic is
    at 2.10 or 2.12, so an upgrade is a change to one series rather than
    one key vanishing and an unrelated one arriving."""
    payload = run_probe_module("dependencies", ["--scope", "all"])
    for observation in payload["observations"]:
        assert "==" not in observation["key"]


def test_dependencies_probe_counts_installed_as_the_one_number() -> None:
    payload = run_probe_module("dependencies", ["--scope", "all"])
    count = next(o for o in payload["observations"] if o["key"] == "count:installed")
    versions = [o for o in payload["observations"] if o["key"].startswith("version:")]
    assert count["value"] == float(len(versions))


def test_dependencies_probe_reports_what_it_could_not_measure() -> None:
    payload = run_probe_module("dependencies", ["--scope", "all"])
    assert any(o["key"] == NOT_MEASURED_KEY for o in payload["observations"])


def test_dependencies_probe_narrows_to_the_declared_closure() -> None:
    declared = run_probe_module("dependencies", ["--root", ".", "--scope", "declared"])
    every = run_probe_module("dependencies", ["--scope", "all"])
    narrowed = next(
        o for o in declared["observations"] if o["key"] == "count:installed"
    )
    total = next(o for o in every["observations"] if o["key"] == "count:installed")
    assert narrowed["value"] <= total["value"]
    assert narrowed["attrs"]["scope"] == "declared"


def test_dependencies_probe_fails_when_nothing_declares_anything(
    tmp_path: Path,
) -> None:
    """Not zero observations. An empty answer would read on the next
    comparison as every dependency having been removed."""
    result = run_probe_raw(
        "dependencies", ["--root", str(tmp_path), "--scope", "declared"]
    )
    assert result.returncode == 2
    assert "declares any dependency" in result.stderr


@pytest.mark.parametrize(
    ("payload", "expected_state"),
    [
        ("", "index"),
        (
            '{"url": "file:///home/someone/proj", "dir_info": {"editable": true}}',
            "editable",
        ),
        ('{"url": "file:///home/someone/proj", "dir_info": {}}', "local path"),
        (
            '{"url": "https://github.com/x/y", "vcs_info": {"vcs": "git"}}',
            "git+https://github.com/x/y",
        ),
        ("not json at all", "direct (unreadable)"),
        ("{}", "direct (no url)"),
    ],
)
def test_origin_state_is_stable_across_machines(
    payload: str, expected_state: str
) -> None:
    """A local absolute path never becomes the state. It is a machine's
    home directory, which has no business in a report, and it would make
    the series machine-specific — the same project checked out elsewhere
    would report a change describing the checkout rather than the
    dependency."""
    from vibe_sentinel.probes.dependencies import origin_of

    state, _detail = origin_of(payload)
    assert state == expected_state
    assert "/home/" not in state


def test_origin_keeps_the_local_path_as_detail() -> None:
    """Out of the state, but not thrown away — a reader still wants to
    know which directory."""
    from vibe_sentinel.probes.dependencies import origin_of

    _state, detail = origin_of(
        '{"url": "file:///home/someone/proj", "dir_info": {"editable": true}}'
    )
    assert detail == "file:///home/someone/proj"


def test_origin_says_unrecorded_when_nothing_could_have_said_otherwise() -> None:
    """An `.egg-info` has nowhere to put a direct URL, so an empty one is
    not evidence of an index. Reporting `index` there would state as a
    fact something nobody measured."""
    from vibe_sentinel.probes.dependencies import origin_of

    assert origin_of("", recorded=False)[0] == "unrecorded"
    assert origin_of("", recorded=True)[0] == "index"
