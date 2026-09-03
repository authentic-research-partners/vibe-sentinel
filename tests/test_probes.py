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
    assert [o["key"] for o in payload["observations"]] == ["silent-total"]
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

    assert len(specific["observations"]) == 1, "one file, counted once"
    assert specific["observations"][0]["attrs"]["category"] == "agent"
    assert general["observations"][0]["attrs"]["category"] == "docs"
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
