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

**When in doubt about a command, do not report.** Every unknown in the
text resolves the quiet way: a token the parser cannot read is not a
package name, a tree with no manifest is not a tree full of undeclared
dependencies, and a name it cannot resolve — ``pip install $PKG`` — is not
a finding. The failure that matters there is the false positive. A gate
that blocks ``uv pip install -e ".[dev]"`` is a gate switched off by
Thursday, and then it guards nothing.

**A manifest that will not parse is not one of those unknowns.** It raises
:class:`ManifestUnreadable`. Answering ``None`` — "no manifest" — made the
gate conclude that nothing was declared, so no install could be
undeclared: with a typo in ``pyproject.toml``, ``pip install reqeusts``
passed triage, and without it the same command was flagged. That is a
substitute answer, not an unknown, and the callers decide what an honest
one is: the hook by mode, as for an unloadable ``[[danger]]`` set, and the
packages gate by failing.
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


class ManifestUnreadable(ValueError):
    """``pyproject.toml`` exists and cannot be read as TOML.

    A ``ValueError``, because that is what every declared table here raises
    when it will not load, and the callers already know what to do with one.
    """


def load_manifest(root: Path) -> dict[str, Any] | None:
    """``pyproject.toml`` as data, or None when there is none to read.

    None and ``{}`` are different answers and the caller must keep them
    apart: a project with no manifest has not declared nothing, it has
    declared nowhere this can look, and no question about its dependencies
    has an answer.

    A manifest that exists and will not parse is neither, and raises
    :class:`ManifestUnreadable` — see the module docstring for what the
    quiet answer used to cost.
    """
    import tomllib

    path = root / "pyproject.toml"
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ManifestUnreadable(
            f"{path} could not be read: {e}. The safety gate and the packages "
            f"gate read it to know what this project declares, and until it "
            f"parses neither can tell a declared dependency from a name that "
            f"came from nowhere. Fix the file, then check it with "
            f"`vibe-sentinel packages`."
        ) from e


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

    None means there is no manifest, which is not the same as an empty
    set; one that will not parse raises :class:`ManifestUnreadable`. A project that declares an empty ``dependencies`` list has stated
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
#: or read from disk. Every form recognised below names ``pip`` or one of
#: the JavaScript package managers, so a command that names none is not
#: one of them.
INSTALL_HINT_RE = re.compile(r"\b(?:pip3?|npm|pnpm|yarn|bun)\b")

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

#: Prefixes that wrap a command without being one — the one answer to that
#: question, read by the safety gate's structural checks too.
WRAPPERS = frozenset({"sudo", "doas", "env", "nohup", "time", "command", "exec"})

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
        tokens[index] in WRAPPERS or _ENV_ASSIGNMENT_RE.match(tokens[index])
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


class Installs(NamedTuple):
    """What one command would install, as far as it can be read."""

    python: tuple[str, ...] = ()
    js: tuple[str, ...] = ()
    unreadable: bool = False
    """It names an installer but will not split into words, so what it
    installs is unknown. A third answer rather than an empty one: bash runs
    ``pip install reqeusts # don't worry`` — the apostrophe is inside a
    comment — while ``shlex`` refuses it, and "installs nothing" would be
    shaped exactly like a command that installs nothing."""


def installs(command: str) -> tuple[str, ...]:
    """The Python package names a command would install, normalized.

    Empty for everything that is not an install this module reads, and for
    an install whose arguments name no bare package: ``-r requirements.txt``,
    ``-e .``, a wheel, a URL, an unexpanded variable. Empty too for a command
    that will not split, which :func:`unreadable_install` tells apart.
    """
    return _read_installs(command).python


def js_installs(command: str) -> tuple[str, ...]:
    """The npm package names a command would add to the project, lowercased.

    ``npm install``, ``npm i`` and ``npm add``; ``pnpm add``, ``pnpm install``
    and ``pnpm i``; ``yarn add``; ``bun add``, ``bun install`` and ``bun i`` —
    with names. The same command without names re-syncs what
    ``package.json`` declares, and names nothing. A global install —
    ``-g``, ``--global``, ``yarn global add`` — is a tool installed on
    purpose outside the project, like ``pipx``, and is not read. ``npx`` and
    ``bunx`` run a package rather than add one. Empty for a command that
    will not split, which :func:`unreadable_install` tells apart.
    """
    return _read_installs(command).js


def unreadable_install(command: str, root: Path | None) -> bool:
    """Whether this command names an installer, cannot be read, and runs in
    a tree with a manifest to compare it against.

    Then which names it installs is unknown, and the safety gate asks the
    model to read the command rather than concluding it installs nothing.
    A tree with no manifest reports nothing, as :func:`undeclared_installs`
    does: nowhere to read a declaration is not a declaration of nothing.
    Raises :class:`ManifestUnreadable` as that does, for the same reason.
    """
    if root is None or not _read_installs(command).unreadable:
        return False
    return declared_names(root) is not None or declared_js_names(root) is not None


def _read_installs(command: str) -> Installs:
    """What one command would install: Python names, npm names, or unknown."""
    if not INSTALL_HINT_RE.search(command):
        return Installs()

    import shlex

    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unbalanced quotes. Reading the halves separately would invent
        # boundaries the shell would not have drawn, so read nothing — and
        # say so, because nothing read is not nothing installed.
        return Installs(unreadable=True)

    names: list[str] = []
    js: list[str] = []
    segment: list[str] = []
    for token in [*tokens, ";"]:
        if token in _SEPARATORS:
            names.extend(_names_in(segment))
            js.extend(_js_names_in(segment))
            segment = []
            continue
        segment.append(token)
    return Installs(tuple(dict.fromkeys(names)), tuple(dict.fromkeys(js)))


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
    install something by name — one read of the manifest for that
    ecosystem: ``pyproject.toml`` for pip, ``package.json`` for npm, pnpm,
    yarn and bun. That read raises :class:`ManifestUnreadable` when the file
    will not parse, so a broken manifest is noticed by the one kind of
    command it affects. A tree with no manifest for the ecosystem reports
    nothing for it: nowhere to read a declaration is not a declaration of
    nothing.
    """
    if root is None or not command:
        return ()
    python, js, _ = _read_installs(command)
    undeclared: list[str] = []
    if python:
        declared = declared_names(root)
        if declared is not None:
            undeclared += [name for name in python if name not in declared]
    if js:
        declared_js = declared_js_names(root)
        if declared_js is not None:
            undeclared += [name for name in js if name not in declared_js]
    return tuple(undeclared)


# ---------------------------------------------------------------------------
# The JavaScript package managers
# ---------------------------------------------------------------------------

#: The subcommand that adds a named package, per manager. ``yarn install``
#: takes no names, so only ``add`` is read for it.
_JS_ADD = {
    "npm": frozenset({"install", "i", "in", "add"}),
    "pnpm": frozenset({"add", "install", "i"}),
    "yarn": frozenset({"add"}),
    "bun": frozenset({"add", "install", "i"}),
}
#: Options whose value is the next token, so it is not read as a name.
_JS_OPTIONS_WITH_A_VALUE = frozenset(
    {
        "--registry", "--prefix", "-w", "--workspace", "--filter", "-F",
        "--cwd", "--tag", "--save-prefix", "--omit", "--include", "--cache",
        "--dir", "-C",
    }
)  # fmt: skip
_JS_GLOBAL = frozenset({"-g", "--global", "--location=global"})
_JS_NOT_A_NAME = ("file:", "link:", "git+", "git:", "github:", "http:", "https:",
                  "workspace:", "portal:", "patch:")  # fmt: skip


def _js_names_in(segment: list[str]) -> list[str]:
    """Every bare npm package name one segment would add to the project."""
    index = 0
    while index < len(segment) and (
        segment[index] in WRAPPERS or _ENV_ASSIGNMENT_RE.match(segment[index])
    ):
        index += 1
    head = segment[index:]
    if len(head) < 2 or head[0] not in _JS_ADD:
        return []
    manager, rest = head[0], head[1:]
    if manager == "yarn" and rest[:1] == ["global"]:
        return []
    verb_at = next((i for i, tok in enumerate(rest) if not tok.startswith("-")), None)
    if verb_at is None or rest[verb_at] not in _JS_ADD[manager]:
        return []
    arguments = rest[:verb_at] + rest[verb_at + 1 :]
    if any(tok in _JS_GLOBAL for tok in arguments):
        return []

    names: list[str] = []
    skip = False
    for token in arguments:
        if skip:
            skip = False
            continue
        if token.startswith("-"):
            skip = token in _JS_OPTIONS_WITH_A_VALUE
            continue
        name = js_package_name(token)
        if name:
            names.append(name)
    return names


def js_package_name(spec: str) -> str:
    """The package an npm spec names, lowercased; '' when it names none.

    ``lodash``, ``lodash@4``, ``@scope/pkg`` and ``@scope/pkg@^1`` are
    names. A path, a tarball, a URL, a git or workspace reference, an alias
    (``foo@npm:bar``) and anything holding a shell expansion are not.
    """
    if (
        not spec
        or spec.startswith((".", "/", "~", *_JS_NOT_A_NAME))
        or "://" in spec
        or "$" in spec
        or "`" in spec
        or "@npm:" in spec
        or spec.endswith((".tgz", ".tar.gz"))
    ):
        return ""
    if spec.startswith("@"):
        scope, _, rest = spec.partition("/")
        if not rest:
            return ""
        return f"{scope}/{rest.split('@', 1)[0]}".lower()
    if "/" in spec:
        return ""  # user/repo — a GitHub shorthand, not a registry name
    return spec.split("@", 1)[0].lower()


def declared_js_names(root: Path) -> frozenset[str] | None:
    """Every npm package ``package.json`` declares, or None when there is none.

    Its dependency tables and its own name. A ``package.json`` that exists
    and will not parse raises :class:`ManifestUnreadable`, for the reason
    ``load_manifest`` does.
    """
    import json

    path = root / "package.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as e:
        raise ManifestUnreadable(
            f"{path} could not be read: {e}. The safety gate reads it to know "
            f"which npm packages this project declares, and until it parses it "
            f"cannot tell a declared dependency from a name that came from "
            f"nowhere. Fix the file, then run the command again."
        ) from e
    if not isinstance(data, dict):
        raise ManifestUnreadable(f"{path} is not a JSON object.")
    names: set[str] = set()
    for table in (
        "dependencies",
        "devDependencies",
        "optionalDependencies",
        "peerDependencies",
    ):
        section = data.get(table)
        if isinstance(section, dict):
            names.update(str(key).lower() for key in section)
    own = data.get("name")
    if isinstance(own, str) and own:
        names.add(own.lower())
    return frozenset(names)
