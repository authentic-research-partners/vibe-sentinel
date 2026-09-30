"""What a project declares, and what a command would install.

The install parser is the part that has to be right in one direction. A
name it misses leaves things as they were before this existed; a name it
invents blocks a command that was fine, and a gate that blocks
``uv pip install -e ".[dev]"`` is uninstalled by Thursday. So the long
table here is the one of commands that must yield *nothing*.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from vibe_sentinel import requirements as rq


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Foo.Bar_baz", "foo-bar-baz"), ("HTTPX", "httpx"), ("  a__b ", "a-b")],
)
def test_normalize_is_pep_503(raw: str, expected: str) -> None:
    assert rq.normalize(raw) == expected


@pytest.mark.parametrize(
    ("raw", "name", "specifier"),
    [
        ("requests", "requests", ""),
        ("requests>=2.0", "requests", ">=2.0"),
        ("uvicorn[standard]>=0.30", "uvicorn", ">=0.30"),
        ('tomli>=2; python_version < "3.11"', "tomli", ">=2"),
    ],
)
def test_parse_reads_name_and_bound(raw: str, name: str, specifier: str) -> None:
    entry = rq.parse(raw, "project")
    assert entry is not None
    assert (entry.name, entry.specifier) == (name, specifier)


def test_an_unreadable_requirement_yields_nothing_rather_than_a_guess() -> None:
    assert rq.parse("!!!", "project") is None
    assert rq.requirement_name("!!!") == ""


# --- what the manifests declare --------------------------------------------


def write_manifest(root: Path, body: str) -> None:
    (root / "pyproject.toml").write_text(textwrap.dedent(body), encoding="utf-8")


def test_every_form_a_manifest_might_use(tmp_path: Path) -> None:
    write_manifest(
        tmp_path,
        """
        [project]
        name = "my-tool"
        dependencies = ["httpx>=0.27", "pydantic"]
        [project.optional-dependencies]
        dev = ["pytest"]
        [dependency-groups]
        lint = ["ruff", {include-group = "dev"}]
        """,
    )
    names = rq.declared_names(tmp_path)
    assert names is not None
    # The project's own distribution is in the set: installing what this
    # tree builds is not installing a name from nowhere.
    assert names == {"httpx", "pydantic", "pytest", "ruff", "my-tool"}


def test_poetry_tables_count_and_the_interpreter_does_not(tmp_path: Path) -> None:
    write_manifest(
        tmp_path,
        """
        [tool.poetry]
        name = "legacy"
        [tool.poetry.dependencies]
        python = "^3.11"
        requests = "^2.31"
        httpx = {version = "^0.27", optional = true}
        [tool.poetry.group.dev.dependencies]
        pytest = "*"
        """,
    )
    assert rq.declared_names(tmp_path) == {"legacy", "requests", "httpx", "pytest"}


def test_no_manifest_is_not_an_empty_manifest(tmp_path: Path) -> None:
    """None and the empty set are different answers.

    A project with no ``pyproject.toml`` has not declared nothing — it has
    declared nowhere this can look. Folding the two together would report
    every install in every plain directory as undeclared.
    """
    assert rq.declared_names(tmp_path) is None
    write_manifest(tmp_path, '[project]\nname = "x"\ndependencies = []\n')
    assert rq.declared_names(tmp_path) == {"x"}


def test_a_broken_manifest_is_an_error_not_an_absent_one(tmp_path: Path) -> None:
    """It used to read as no manifest, and that is a substitute answer: no
    manifest means nothing is declared, so no install can be undeclared.
    With the typo that sat in this repository's own pyproject.toml,
    `pip install reqeusts` passed triage; without it, it was flagged."""
    write_manifest(tmp_path, "authors = authors = [\n")
    with pytest.raises(rq.ManifestUnreadable, match="pyproject.toml could not be read"):
        rq.declared_names(tmp_path)
    with pytest.raises(rq.ManifestUnreadable, match="vibe-sentinel packages"):
        rq.undeclared_installs("pip install reqeusts", tmp_path)


def test_a_broken_manifest_costs_nothing_to_a_command_that_installs_nothing(
    tmp_path: Path,
) -> None:
    """The manifest is read only for a command that installs something by
    name, so a broken one is noticed by the one kind of command it affects."""
    write_manifest(tmp_path, "authors = authors = [\n")
    assert rq.undeclared_installs("ls -la", tmp_path) == ()
    assert rq.undeclared_installs("pip install $PKG", tmp_path) == ()


# --- what a command would install ------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("pip install requests", ("requests",)),
        ("pip3 install --upgrade httpx pydantic", ("httpx", "pydantic")),
        ("uv pip install requests", ("requests",)),
        ("python -m pip install loguru", ("loguru",)),
        ("python3.13 -m pip install loguru", ("loguru",)),
        ("sudo pip install foo", ("foo",)),
        ("FOO=bar pip install foo", ("foo",)),
        ("cd /tmp && pip install colorama", ("colorama",)),
        ('pip install "uvicorn[standard]>=0.30"', ("uvicorn",)),
        ('pip install "requests>=2,<3"', ("requests",)),
        ("uv pip install --index-url https://x/simple mypkg", ("mypkg",)),
    ],
)
def test_the_names_an_install_would_add(
    command: str, expected: tuple[str, ...]
) -> None:
    assert rq.installs(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        # The two that matter most: re-syncing what the manifest already
        # says, in the exact forms this project's own setup instructions use.
        'uv pip install -e ".[dev]"',
        "uv pip install -e .",
        "pip install -r requirements.txt",
        "pip install -c constraints.txt -r requirements.txt",
        "pip install .",
        # Installers that write the dependency down. That is the
        # remediation, not the fault.
        "uv add requests",
        "poetry add requests",
        "pdm add requests",
        # A tool installed on purpose outside the project's dependencies.
        "pipx install ruff",
        # Not a name this can resolve, so not a name it reports.
        "pip install $PKG",
        "pip install ${PACKAGE}",
        "pip install git+https://github.com/psf/requests",
        "pip install https://example.com/x-1.0-py3-none-any.whl",
        "pip install ./dist/x-1.0.tar.gz",
        "pip install",
        "pip install 'unbalanced",
        # Commands that merely mention one.
        "grep -rn 'pip install' README.md",
        "echo pip install requests > notes.txt",
        "npm install lodash",
        "pytest tests/ -q",
    ],
)
def test_commands_that_install_no_name_this_can_read(command: str) -> None:
    assert rq.installs(command) == ()


def test_undeclared_is_the_difference_between_the_two(tmp_path: Path) -> None:
    write_manifest(
        tmp_path,
        '[project]\nname = "mine"\ndependencies = ["httpx>=0.27"]\n',
    )
    assert rq.undeclared_installs("uv pip install httpx", tmp_path) == ()
    assert rq.undeclared_installs('uv pip install -e ".[dev]"', tmp_path) == ()
    assert rq.undeclared_installs("uv pip install mine", tmp_path) == ()
    assert rq.undeclared_installs("uv pip install requests", tmp_path) == ("requests",)
    assert rq.undeclared_installs("pip install httpx requests", tmp_path) == (
        "requests",
    )


def test_a_tree_with_no_manifest_reports_nothing(tmp_path: Path) -> None:
    assert rq.undeclared_installs("pip install requests", tmp_path) == ()
    assert rq.undeclared_installs("pip install requests", None) == ()


def test_the_costly_imports_are_deferred() -> None:
    """The docstring's claim, asserted.

    ``safety.triage`` imports this module in front of every tool call, so
    ``tomllib`` (4.1 ms) and ``shlex`` must not load until a command that
    really is an install asks for them. Nothing else would fail if one
    crept up to module scope.
    """
    code = (
        "import sys, vibe_sentinel.requirements as r; "
        "print(sorted({'tomllib', 'shlex'} & set(sys.modules)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"


# --- the JavaScript package managers ---------------------------------------------


def write_package_json(root: Path, body: str) -> None:
    (root / "package.json").write_text(body, encoding="utf-8")


_PACKAGE_JSON = (
    '{"name": "scene", "dependencies": {"lodash": "^4", "@types/node": "^20"},'
    ' "devDependencies": {"vitest": "^1"}}'
)


@pytest.mark.parametrize(
    ("command", "undeclared"),
    [
        ("npm install lodahs", ("lodahs",)),
        ("npm add @types/nodee", ("@types/nodee",)),
        ("pnpm add reacct", ("reacct",)),
        ("yarn add expresss@4", ("expresss",)),
        ("bun add zodd", ("zodd",)),
        ("npm i -w packages/web reacct", ("reacct",)),
        ("cd web && npm install lodahs", ("lodahs",)),
    ],
)
def test_an_npm_family_install_of_an_undeclared_name_is_found(
    tmp_path: Path, command: str, undeclared: tuple[str, ...]
) -> None:
    write_package_json(tmp_path, _PACKAGE_JSON)
    assert rq.undeclared_installs(command, tmp_path) == undeclared


@pytest.mark.parametrize(
    "command",
    [
        "npm i lodash",
        "npm i --save-dev vitest",
        "npm install",
        "pnpm install",
        "yarn install",
        "bun install",
        "npm install -g typescript",
        "yarn global add typescript",
        "npm i ./local-pkg",
        "npm i github:user/repo",
        "npm i user/repo",
        "npm i foo@npm:bar",
        "npx create-vite app",
    ],
)
def test_a_sync_a_global_tool_or_a_non_name_is_not_undeclared(
    tmp_path: Path, command: str
) -> None:
    """Re-syncing, a tool installed on purpose outside the project, and a
    spec that is not a registry name are the npm twins of `pip install -e .`
    and `pipx install`."""
    write_package_json(tmp_path, _PACKAGE_JSON)
    assert rq.undeclared_installs(command, tmp_path) == ()


def test_no_package_json_means_nothing_to_compare_against(tmp_path: Path) -> None:
    assert rq.undeclared_installs("npm install lodahs", tmp_path) == ()


def test_a_broken_package_json_is_an_error_not_an_absent_one(tmp_path: Path) -> None:
    """The same rule as a broken pyproject.toml: unreadable is not undeclared."""
    write_package_json(tmp_path, '{"dependencies": {"lodash": }')
    with pytest.raises(rq.ManifestUnreadable, match="package.json could not be read"):
        rq.undeclared_installs("npm install lodahs", tmp_path)


# --- an install nobody can read --------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "pip install reqeusts # don't worry",
        "yarn add lodahs # it's fine",
    ],
)
def test_an_install_that_will_not_split_is_unreadable_not_empty(
    tmp_path: Path, command: str
) -> None:
    """bash runs both — the apostrophe is inside a comment — and shlex will
    not split either. No names can be read, and that is a third answer: an
    empty one is shaped exactly like a command that installs nothing."""
    write_manifest(tmp_path, '[project]\nname = "mine"\ndependencies = []\n')
    write_package_json(tmp_path, _PACKAGE_JSON)
    assert rq.installs(command) == ()
    assert rq.js_installs(command) == ()
    assert rq.unreadable_install(command, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "Don\'t pin npm packages"',  # balanced: it splits
        "echo don't",  # names no installer
        "pip install requests",  # read, not unreadable
    ],
)
def test_a_command_that_splits_or_names_no_installer_is_not_unreadable(
    tmp_path: Path, command: str
) -> None:
    write_manifest(tmp_path, '[project]\nname = "mine"\ndependencies = []\n')
    assert not rq.unreadable_install(command, tmp_path)


def test_an_unreadable_install_with_no_manifest_reports_nothing(
    tmp_path: Path,
) -> None:
    """Nowhere to read a declaration is not a declaration of nothing — the
    same rule the readable case keeps."""
    assert not rq.unreadable_install("pip install reqeusts # don't worry", tmp_path)
    assert not rq.unreadable_install("pip install reqeusts # don't worry", None)
