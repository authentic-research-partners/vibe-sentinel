"""What a project writes down, and what a command would install.

Two readers with one question between them. ``packages.py`` asks what the
manifests declare so it can compare that against the environment; the
safety gate asks the same thing in front of a tool call, to tell an
install that re-syncs declared dependencies from one that adds a name
nobody wrote down. A second walker over ``pyproject.toml`` would be a
second thing to update when a manifest shape changes, and the two would
disagree the first time one was missed — so the walk lives here, once,
and both import it.

**Stdlib only, and lazily.** :func:`~vibe_sentinel.safety.triage` reaches
for this in front of every tool call, so nothing here may cost what
``packages.py`` costs: it imports httpx and pydantic at module scope, and
either would blow the hook's whole budget. ``tomllib`` (4.1 ms) and
``shlex`` (0.2 ms) are imported inside the functions that need them,
because the common command is not an install and should pay for neither.

**When in doubt, do not report.** Every unknown here resolves the quiet
way: a token the parser cannot read is not a package name, a tree with no
manifest is not a tree full of undeclared dependencies, and a name it
cannot resolve — ``pip install $PKG`` — is not a finding. The failure
that matters is the false positive. A gate that blocks
``uv pip install -e ".[dev]"`` is a gate switched off by Thursday, and
then it guards nothing; a missed install leaves things exactly as they
were before this existed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator


def normalize(name: str) -> str:
    """PEP 503 normalisation. ``Foo.Bar_baz`` and ``foo-bar-baz`` are one package."""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


#: A PEP 508 requirement, read far enough to know its name and its bound.
#: Deliberately not a full parser: those two facts survive a shape this
#: simple, and a requirement it cannot read at all yields nothing rather
#: than a guessed name.
REQUIREMENT_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")


class Declared(NamedTuple):
    """One dependency as a manifest wrote it.

    A ``NamedTuple`` rather than a pydantic model because this is read on
    the hook's path, where a model costs ~55 ms — the same reason
    ``journal.py`` and ``safety.py`` carry their own waivers. It is not a
    boundary type: ``packages.Requirement`` is the model that crosses one,
    and it is built from these fields.
    """

    name: str  # normalized
    raw: str
    specifier: str  # "" when no version bound was given
    marker: str
    group: str  # project | extra:<name> | group:<name> | poetry[:<group>]


def parse(raw: str, group: str) -> Declared | None:
    """One requirement string, or None when it is not readable as one."""
    head, _, marker = raw.partition(";")
    match = REQUIREMENT_RE.match(head)
    if not match:
        return None
    name, _extras, specifier = match.groups()
    return Declared(
        name=normalize(name),
        raw=raw.strip(),
        specifier=specifier.strip(),
        marker=marker.strip(),
        group=group,
    )


def requirement_name(raw: str) -> str:
    """Just the distribution name, normalized. ``''`` when unreadable."""
    parsed = parse(raw, "")
    return parsed.name if parsed else ""


# ---------------------------------------------------------------------------
# What the manifests declare
# ---------------------------------------------------------------------------


def load_manifest(root: Path) -> dict[str, Any] | None:
    """``pyproject.toml`` as data, or None when there is none to read.

    None and ``{}`` are different answers and the caller must keep them
    apart: a project with no manifest has not declared nothing, it has
    declared nowhere this can look, and no question about its dependencies
    has an answer.
    """
    import tomllib

    path = root / "pyproject.toml"
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None


def iter_declared(data: dict[str, Any]) -> Iterator[Declared]:
    """Every dependency a parsed manifest writes down, in every form it might use."""
    project = data.get("project", {}) or {}
    for raw in project.get("dependencies", []) or []:
        entry = parse(str(raw), "project")
        if entry:
            yield entry
    for extra, reqs in (project.get("optional-dependencies", {}) or {}).items():
        for raw in reqs or []:
            entry = parse(str(raw), f"extra:{extra}")
            if entry:
                yield entry

    for group, reqs in (data.get("dependency-groups", {}) or {}).items():
        for raw in reqs or []:
            # PEP 735 allows {include-group = "other"}; the names it pulls in are
            # already read from that other group's own list.
            if not isinstance(raw, str):
                continue
            entry = parse(raw, f"group:{group}")
            if entry:
                yield entry

    yield from _poetry(data.get("tool", {}) or {})


def _poetry(tool: dict[str, Any]) -> Iterator[Declared]:
    """Poetry's ``name = constraint`` tables, including its dependency groups."""
    poetry = tool.get("poetry", {}) or {}
    tables: list[tuple[str, dict[str, Any]]] = [
        ("poetry", poetry.get("dependencies", {}) or {})
    ]
    for group_name, group in (poetry.get("group", {}) or {}).items():
        tables.append((f"poetry:{group_name}", group.get("dependencies", {}) or {}))

    for group_label, table in tables:
        for name, constraint in table.items():
            if normalize(name) == "python":  # the interpreter, not a package
                continue
            if isinstance(constraint, dict):
                spec = str(constraint.get("version", ""))
            else:
                spec = str(constraint)
            # Poetry writes "*" for "any version", which is the same statement as
            # an empty specifier and should read as unconstrained either way.
            yield Declared(
                name=normalize(name),
                raw=f"{name} = {constraint!r}",
                specifier="" if spec in ("", "*") else spec,
                marker="",
                group=group_label,
            )


def name_of(data: dict[str, Any]) -> str:
    """The distribution a parsed manifest builds, normalized. '' when it builds none."""
    name = (data.get("project", {}) or {}).get("name")
    if not name:
        name = ((data.get("tool", {}) or {}).get("poetry", {}) or {}).get("name")
    return normalize(str(name)) if name else ""


def declared_names(root: Path) -> frozenset[str] | None:
    """Every package name this project writes down, or None when it writes none.

    The project's own distribution is in the set: installing the thing
    this tree builds is not installing a name from nowhere.

    None means no manifest was readable, which is not the same as an empty
    set. A project that declares an empty ``dependencies`` list has stated
    something; a project with no ``pyproject.toml`` has not, and a caller
    that folds the two together reports every install in every plain
    directory as undeclared.
    """
    data = load_manifest(root)
    if data is None:
        return None
    names = {entry.name for entry in iter_declared(data)}
    own = name_of(data)
    if own:
        names.add(own)
    return frozenset(names)


# ---------------------------------------------------------------------------
# What a command would install
# ---------------------------------------------------------------------------

#: The cheap prefilter, run on the raw text before anything is tokenized
#: or read from disk. Every form recognised below names ``pip``, so a
#: command that does not is not one of them.
INSTALL_HINT_RE = re.compile(r"\bpip3?\b")

#: Installers that add a package to the environment and write it down
#: nowhere. ``uv add``, ``poetry add`` and ``pdm add`` are deliberately
#: absent: they edit the manifest, which is the remediation rather than
#: the fault. So are ``pipx install`` — a tool installed on purpose
#: outside the project's dependencies — and ``conda install``, whose names
#: are its own and are not what ``pyproject.toml`` declares.
_PIP = ("pip", "pip3")
_PYTHON_RE = re.compile(r"^python(?:3(?:\.\d+)?)?$")

#: Command separators. Each side is its own command, and only one of them
#: may be the install.
_SEPARATORS = frozenset({"&&", "||", ";", "|", "&"})

#: Prefixes that wrap a command without being one.
_WRAPPERS = frozenset({"sudo", "doas", "env", "nohup", "time", "command", "exec"})

_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

#: Options whose *value* is the next token. Without these, `-r` would be
#: skipped as an option and `requirements.txt` read as a package name.
_OPTIONS_WITH_A_VALUE = frozenset(
    {
        "-r", "--requirement", "-c", "--constraint", "-e", "--editable",
        "-i", "--index-url", "--extra-index-url", "-f", "--find-links",
        "-t", "--target", "--prefix", "--root", "--src", "--log",
        "-p", "--python", "--upgrade-strategy", "--no-binary",
        "--only-binary", "--platform", "--implementation", "--abi",
        "--cache-dir", "--proxy", "--retries", "--timeout",
        "--exists-action", "--trusted-host", "--config-settings", "-C",
        "--build-option", "--global-option", "--hash", "--index-strategy",
        "--link-mode", "--resolution", "--refresh-package", "--extra",
        "--constraints", "--overrides", "--no-emit-package",
    }
)  # fmt: skip


def _is_not_a_name(token: str) -> bool:
    """Whether this argument is something other than a bare package name.

    A path, an archive, a URL, a VCS reference, or anything still holding
    a shell expansion this cannot resolve. ``.``, ``./`` and ``".[dev]"``
    all land here, which is what keeps an editable install of the project
    itself from reading as a package nobody declared.
    """
    return (
        token.startswith((".", "/", "~", "git+", "hg+", "bzr+", "svn+"))
        or "/" in token
        or "://" in token
        or "$" in token
        or "`" in token
        or token.endswith((".whl", ".tar.gz", ".tgz", ".zip", ".txt"))
    )


def _install_arguments(tokens: list[str]) -> list[str] | None:
    """The arguments of a pip-style install in one segment, or None.

    None means this segment is not an install this module claims to read —
    which includes every installer that records what it installed.
    """
    index = 0
    while index < len(tokens) and (
        tokens[index] in _WRAPPERS or _ENV_ASSIGNMENT_RE.match(tokens[index])
    ):
        index += 1
    head = tokens[index:]
    if not head:
        return None
    if head[0] in _PIP and head[1:2] == ["install"]:
        return head[2:]
    if head[:3] == ["uv", "pip", "install"]:
        return head[3:]
    if _PYTHON_RE.match(head[0]) and head[1:4] == ["-m", "pip", "install"]:
        return head[4:]
    return None


def installs(command: str) -> tuple[str, ...]:
    """The package names a command would install, normalized.

    Empty for everything that is not an install this module reads, and for
    an install whose arguments name no bare package: ``-r requirements.txt``,
    ``-e .``, a wheel, a URL, an unexpanded variable.
    """
    if not INSTALL_HINT_RE.search(command):
        return ()

    import shlex

    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unbalanced quotes. Reading the halves separately would invent
        # boundaries the shell would not have drawn, so read nothing.
        return ()

    names: list[str] = []
    segment: list[str] = []
    for token in [*tokens, ";"]:
        if token in _SEPARATORS:
            names.extend(_names_in(segment))
            segment = []
            continue
        segment.append(token)
    return tuple(dict.fromkeys(names))


def _names_in(segment: list[str]) -> list[str]:
    """Every bare package name in one segment's install arguments."""
    arguments = _install_arguments(segment)
    if arguments is None:
        return []
    names: list[str] = []
    skip = False
    for token in arguments:
        if skip:
            skip = False
            continue
        if token.startswith("-"):
            skip = token in _OPTIONS_WITH_A_VALUE
            continue
        if _is_not_a_name(token):
            continue
        name = requirement_name(token)
        if name:
            names.append(name)
    return names


def undeclared_installs(command: str, root: Path | None) -> tuple[str, ...]:
    """Names this command would install that no manifest in ``root`` declares.

    The whole check, in the order that keeps it cheap: a regex over the
    text, then a tokenize, then — only for a command that really does
    install something by name — one read of ``pyproject.toml``.
    """
    if root is None or not command:
        return ()
    candidates = installs(command)
    if not candidates:
        return ()
    declared = declared_names(root)
    if declared is None:
        return ()
    return tuple(name for name in candidates if name not in declared)
