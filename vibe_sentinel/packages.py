"""Dependency provenance: does this package exist, and who put it here?

**Why this lives in vibe-sentinel.** The licence gate next door asks what terms a
dependency arrives under. This one asks a question that comes first: is the dependency
*real*, and did anyone decide to add it.

The reason it needs asking is specific to how code is written now. A coding model that
does not know a library will invent one, confidently, with a plausible name and a
plausible import. Spracklen et al. (USENIX Security '25) generated 576,000 code samples
across 16 models and found hallucinated package names in 5.2% of commercial-model output
and 21.7% of open-source-model output -- 205,474 unique invented names. The follow-up
measurement on the 2026 frontier cohort puts the range at 4.62%-6.10%: better, not gone.

Invented names would be a harmless typo if they stayed invented. They do not. The names
repeat -- the same prompt produces the same fiction across runs and across vendors -- so
they are harvestable, and registering one turns every future hallucination into a working
install. That is *slopsquatting*, and the shape has been demonstrated repeatedly: an empty
package registered under a name models kept inventing took 30,000 downloads in three
months, and appeared in the install instructions of a major vendor's public repository.

So the checks here are about provenance, and every one of them is mechanical:

  ``phantom``       your source imports a name that resolves to nothing at all
  ``undeclared``    you import it, it is installed, nothing declares it
  ``orphan``        it is installed, nothing declares it, nothing requires it
  ``unconstrained`` declared with no version bound, so any future release satisfies it
  ``near-miss``     two installed names one edit apart -- the typosquat shape
  ``unregistered``  (``--online``) the name is not on PyPI at all
  ``squatted``      (``--online``) the name IS on PyPI, registered days ago
  ``newborn``       (``--online``) a dependency whose first release is very recent
  ``unchecked``     the online confirmation was asked for and did not happen

``squatted`` is the one worth reading twice. Offline, a phantom import is just a name your
code cannot resolve. Online, the interesting answer is not "no such package" -- it is "yes,
that package exists, and it was created last Tuesday".

**What this does not do.** It does not compare your dependency names against a shipped
corpus of popular packages. A published list of "names close to real ones" is a published
rule, with the problem every published rule has here (see the README): it enters the next
training corpus and stops measuring what it stood for. ``near-miss`` therefore compares
your environment against *itself*, which needs no corpus and cannot be trained around.

**Which environment.** Everything is read from the interpreter running this code --
``importlib.metadata`` walks that interpreter's ``sys.path``. A conda env, a venv, a
poetry or uv project env all work, and all mean "run it with that env's python". The
environment is identified and recorded rather than assumed, because measuring env A one
run and env B the next manufactures a diff out of nothing.
"""

from __future__ import annotations

import ast
import fnmatch
import importlib.metadata as md
import importlib.util
import os
import re
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, ConfigDict

from vibe_sentinel.paths import CONFIG_FILENAME
from vibe_sentinel.pins import check_pins

if TYPE_CHECKING:  # pragma: no cover - the offline path must not pay for this
    from vibe_sentinel.config import SentinelConfig

#: The project's own config file. A ``[packages]`` table here is the normal place
#: for the policy — one config file for the whole tool.
PROJECT_CONFIG = Path(CONFIG_FILENAME)

#: Standalone policy file, checked when the project config has no ``[packages]``
#: table. For an organisation shipping one policy across many repos.
POLICY_PATH = Path("security") / "package-policy.toml"

#: PyPI's per-project JSON. Queried only under ``--online``, and only for names
#: that are already suspect or already written down in pyproject.toml.
PYPI_JSON = "https://pypi.org/pypi/{name}/json"

#: Directories that are not this project's source, and whose contents would
#: otherwise be read as its imports.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        ".nox",
        "dist",
        "build",
        "site-packages",
        ".vibe-sentinel",
    }
)

#: Installed by pip, venv and conda themselves. No pyproject declares them and
#: their absence from one is not a finding.
DEFAULT_IGNORE = frozenset(
    {"pip", "setuptools", "wheel", "uv", "distribute", "pkg-resources", "pkg_resources"}
)

#: Shortest name compared for near-misses. Below this, one edit separates far too
#: many unrelated real names (``six`` / ``sax`` / ``sox``).
_MIN_NEAR_MISS_LEN = 5

#: Risk vocabulary, most serious first. An observation carries one risk, so when a
#: package trips several checks this order decides which one is recorded.
RISK_ORDER: tuple[str, ...] = (
    "squatted",
    "unregistered",
    "phantom",
    "near-miss",
    "newborn",
    "undeclared",
    "orphan",
    "uninstalled",
    "unconstrained",
    "unchecked",
)


def severity_rank(kind: str) -> int:
    """Position of ``kind`` in :data:`RISK_ORDER`; unknown kinds sort last."""
    try:
        return RISK_ORDER.index(kind)
    except ValueError:
        return len(RISK_ORDER)


def normalize(name: str) -> str:
    """PEP 503 normalisation. ``Foo.Bar_baz`` and ``foo-bar-baz`` are one package."""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


# --------------------------------------------------------------------------------------
# The environment being measured
# --------------------------------------------------------------------------------------
#
# Recorded, never assumed. The failure this prevents is the one `vibe-sentinel
# parameters` exists for on the probe side: a run measured against a different
# environment invents its entire diff, and without the environment written down the
# result reads as real drift.


class Environment(BaseModel):
    """Which interpreter's installed packages were read."""

    model_config = ConfigDict(frozen=True)

    kind: str  # conda | venv | poetry | uv | system
    name: str
    prefix: str
    python: str
    executable: str

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.name}"


def _venv_flavour(prefix: Path) -> str:
    """Distinguish plain venv from the tools that create one.

    ``pyvenv.cfg`` is written by whoever built the environment and names itself:
    uv writes a ``uv =`` key, poetry keeps its environments under a ``pypoetry``
    path. Nothing here changes what is measured — it only makes the record say
    which tool the reader should go and ask.
    """
    if os.environ.get("POETRY_ACTIVE") or "pypoetry" in prefix.as_posix():
        return "poetry"
    config = prefix / "pyvenv.cfg"
    if config.is_file():
        text = config.read_text(encoding="utf-8", errors="replace").lower()
        if re.search(r"^\s*uv\s*=", text, re.MULTILINE):
            return "uv"
    return "venv"


def environment() -> Environment:
    """Identify the interpreter whose packages this module is reading."""
    prefix = Path(sys.prefix)
    conda = os.environ.get("CONDA_PREFIX")
    version = ".".join(str(p) for p in sys.version_info[:3])

    if conda and Path(conda) == prefix:
        kind = "conda"
        name = os.environ.get("CONDA_DEFAULT_ENV") or prefix.name
    elif sys.prefix != sys.base_prefix:
        kind = _venv_flavour(prefix)
        name = prefix.name
    else:
        kind = "system"
        name = prefix.name

    return Environment(
        kind=kind,
        name=name,
        prefix=prefix.as_posix(),
        python=version,
        executable=sys.executable,
    )


# --------------------------------------------------------------------------------------
# What the project declares
# --------------------------------------------------------------------------------------
#
# Four manifests are read, because "declared" has four spellings and a project that
# uses one still gets the same answer:
#
#   [project.dependencies]              PEP 621 — the standard
#   [project.optional-dependencies]     PEP 621 extras
#   [dependency-groups]                 PEP 735 — dev groups that never ship
#   [tool.poetry...dependencies]        poetry's own, still the shape of many repos
#
# Lock files are deliberately NOT read. A lock says what an install *would* produce;
# the installed environment says what will actually execute, and those differ exactly
# when it matters.

_REQ_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")


class Requirement(BaseModel):
    """One declared dependency, as written."""

    model_config = ConfigDict(frozen=True)

    name: str  # normalized
    raw: str
    specifier: str  # "" when no version bound was given
    marker: str
    group: str  # project | extra:<name> | group:<name> | poetry[:<group>]


def parse_requirement(raw: str, group: str) -> Requirement | None:
    """Parse a PEP 508 requirement far enough to know its name and its bound.

    Deliberately not a full PEP 508 parser: the two facts wanted here are the
    distribution name and whether any version bound was stated, and both survive
    a shape this simple. A requirement it cannot read at all returns None rather
    than guessing a name.
    """
    head, _, marker = raw.partition(";")
    match = _REQ_RE.match(head)
    if not match:
        return None
    name, _extras, specifier = match.groups()
    if "@" in specifier:  # direct reference: name @ https://... or name @ file://
        specifier = specifier.strip()
    return Requirement(
        name=normalize(name),
        raw=raw.strip(),
        specifier=specifier.strip(),
        marker=marker.strip(),
        group=group,
    )


def _poetry_requirements(tool: dict[str, Any]) -> list[Requirement]:
    """Poetry's ``name = constraint`` tables, including its dependency groups."""
    poetry = tool.get("poetry", {})
    out: list[Requirement] = []
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
            out.append(
                Requirement(
                    name=normalize(name),
                    raw=f"{name} = {constraint!r}",
                    specifier="" if spec in ("", "*") else spec,
                    marker="",
                    group=group_label,
                )
            )
    return out


def declared_requirements(root: Path) -> list[Requirement]:
    """Every dependency this project writes down, in every manifest it might use."""
    path = root / "pyproject.toml"
    if not path.is_file():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out: list[Requirement] = []

    project = data.get("project", {})
    for raw in project.get("dependencies", []) or []:
        req = parse_requirement(str(raw), "project")
        if req:
            out.append(req)
    for extra, reqs in (project.get("optional-dependencies", {}) or {}).items():
        for raw in reqs or []:
            req = parse_requirement(str(raw), f"extra:{extra}")
            if req:
                out.append(req)

    for group, reqs in (data.get("dependency-groups", {}) or {}).items():
        for raw in reqs or []:
            # PEP 735 allows {include-group = "other"}; the names it pulls in are
            # already read from that other group's own list.
            if not isinstance(raw, str):
                continue
            req = parse_requirement(raw, f"group:{group}")
            if req:
                out.append(req)

    out.extend(_poetry_requirements(data.get("tool", {}) or {}))
    return out


def project_name(root: Path) -> str:
    """The distribution this repo builds, normalized. '' when it builds none."""
    path = root / "pyproject.toml"
    if not path.is_file():
        return ""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    name = (data.get("project", {}) or {}).get("name")
    if not name:
        name = ((data.get("tool", {}) or {}).get("poetry", {}) or {}).get("name")
    return normalize(str(name)) if name else ""


# --------------------------------------------------------------------------------------
# What the source actually imports
# --------------------------------------------------------------------------------------


class SourceImport(BaseModel):
    """One top-level module name imported by this project's own code."""

    model_config = ConfigDict(frozen=True)

    module: str
    sites: tuple[str, ...]  # "path/to/file.py:12"


def first_party(root: Path) -> frozenset[str]:
    """Import names that belong to this repo rather than to a dependency.

    A directory with ``__init__.py``, a module at the top level, and the built
    distribution's own name with hyphens turned back into underscores.
    """
    names: set[str] = set()
    for child in root.iterdir() if root.is_dir() else []:
        if child.name in SKIP_DIRS or child.name.startswith("."):
            continue
        if child.is_dir() and (child / "__init__.py").is_file():
            names.add(child.name)
        elif child.is_file() and child.suffix == ".py":
            names.add(child.stem)
    dist = project_name(root)
    if dist:
        names.add(dist.replace("-", "_"))
    return frozenset(names)


def source_imports(root: Path, pattern: str = "*.py") -> list[SourceImport]:
    """Top-level module names imported anywhere in the project's own source.

    Relative imports are skipped: they are internal by definition and belong to
    the module-organization probe, not here.
    """
    sites: dict[str, list[str]] = {}
    for path in sorted(root.rglob(pattern)):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError):
            # A file this module cannot parse is a file whose imports it cannot
            # claim to have read. Skipped, and the caller counts what was read.
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    sites.setdefault(top, []).append(f"{rel.as_posix()}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                top = node.module.split(".")[0]
                sites.setdefault(top, []).append(f"{rel.as_posix()}:{node.lineno}")
    return [
        SourceImport(module=module, sites=tuple(sorted(set(found))))
        for module, found in sorted(sites.items())
    ]


# --------------------------------------------------------------------------------------
# What is installed
# --------------------------------------------------------------------------------------


class Installed(BaseModel):
    """One distribution present in the environment being measured."""

    model_config = ConfigDict(frozen=True)

    name: str  # normalized
    version: str
    requires: tuple[str, ...] = ()  # normalized, extras-only requirements dropped
    installer: str = ""  # pip, uv, conda, ... — '' when the wheel recorded none
    direct_url: str = ""  # set when installed from a path/URL, not an index
    summary: str = ""
    """The distribution's own one-line description, as it shipped it.

    Read for the near-miss adjudication and nothing else: two names one
    edit apart that describe two different things are two packages, and
    that is the fact an edit distance cannot reach."""


_EXTRA_MARKER = re.compile(r";.*\bextra\s*==", re.IGNORECASE)


def _requires(dist: md.Distribution) -> tuple[str, ...]:
    """Names this distribution needs unconditionally.

    Requirements guarded by ``extra == "..."`` are dropped: they arrive only when
    somebody asks for that extra, and treating them as always-required would put
    half of PyPI in the closure and hide real orphans behind it.
    """
    out: set[str] = set()
    for raw in dist.metadata.get_all("Requires-Dist") or []:
        text = str(raw)
        if _EXTRA_MARKER.search(text):
            continue
        req = parse_requirement(text, "installed")
        if req:
            out.add(req.name)
    return tuple(sorted(out))


def _dist_text(dist: md.Distribution, name: str) -> str:
    try:
        return (dist.read_text(name) or "").strip()
    except OSError:
        return ""


def installed_distributions() -> dict[str, Installed]:
    """Every distribution on this interpreter's path, keyed by normalized name."""
    out: dict[str, Installed] = {}
    for dist in md.distributions():
        if not (dist.metadata and dist.metadata.get("Name")):
            continue
        name = normalize(str(dist.metadata.get("Name")))
        # Two site-packages entries for one name (a stale .egg-info beside a
        # .dist-info) resolve to whichever comes first on the path, which is what
        # would be imported.
        if name in out:
            continue
        out[name] = Installed(
            name=name,
            version=str(dist.metadata.get("Version") or ""),
            requires=_requires(dist),
            installer=_dist_text(dist, "INSTALLER"),
            direct_url=_dist_text(dist, "direct_url.json"),
            summary=str(dist.metadata.get("Summary") or "")[:200],
        )
    return out


def requirement_closure(
    declared: set[str], installed: dict[str, Installed]
) -> frozenset[str]:
    """Declared names plus everything they transitively require.

    Anything installed and outside this set is in the environment for a reason
    nothing in the project records.
    """
    seen: set[str] = set()
    stack = [name for name in declared]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        dist = installed.get(current)
        if dist:
            stack.extend(dist.requires)
    return frozenset(seen)


def import_owners() -> dict[str, tuple[str, ...]]:
    """Top-level import name -> the distribution(s) providing it."""
    return {
        module: tuple(sorted(normalize(d) for d in dists))
        for module, dists in md.packages_distributions().items()
    }


def is_importable(module: str) -> bool:
    """Whether ``module`` can be found on the path at all.

    The backstop behind the metadata lookup: namespace packages, ``.pth``
    installs, editable installs and C extensions are importable without ever
    appearing in ``packages_distributions``. Reporting one of those as a
    hallucinated name would be exactly the false alarm that gets a gate switched
    off, so the cheap import check is asked before anything is reported.

    ``ImportError`` is deliberately not caught. Only top-level names reach here,
    and for those ``find_spec`` returns None rather than raising — the raising
    case is a dotted name whose *parent* is missing. Catching it anyway would
    trip this project's own ban on ``except ImportError`` for exactly the right
    reason: the handler would be dead code standing in for a failure that cannot
    occur.
    """
    try:
        return importlib.util.find_spec(module) is not None
    except (ValueError, AttributeError, TypeError):
        return False


# --------------------------------------------------------------------------------------
# Near misses
# --------------------------------------------------------------------------------------


def _within_one_edit(a: str, b: str) -> bool:
    """True when one insertion, deletion or substitution turns ``a`` into ``b``."""
    if abs(len(a) - len(b)) > 1:
        return False
    if a == b:
        return False
    if len(a) > len(b):
        a, b = b, a
    # a is now the shorter (or equal-length) string.
    i = j = 0
    edited = False
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        if edited:
            return False
        edited = True
        if len(a) == len(b):
            i += 1
        j += 1
    return True


_DIGITS = re.compile(r"\d+")


def near_misses(names: list[str]) -> list[tuple[str, str]]:
    """Pairs of installed names one edit apart — the typosquat shape, locally.

    Names differing only in digits are not reported. ``httpx`` / ``httpx2`` and
    ``httpcore`` / ``httpcore2`` are one edit apart and both entirely legitimate:
    a trailing number is how a project ships a rewrite, not how a name is
    mistyped. Both pairs are installed in this repo's own environment, which is
    how the rule got written.
    """
    candidates = sorted(n for n in names if len(n) >= _MIN_NEAR_MISS_LEN)
    out: list[tuple[str, str]] = []
    for index, first in enumerate(candidates):
        for second in candidates[index + 1 :]:
            if len(second) - len(first) > 1:
                break  # sorted by name, not length — cheap reject only
            if not _within_one_edit(first, second):
                continue
            if _DIGITS.sub("", first) == _DIGITS.sub("", second):
                continue
            out.append((first, second))
    return out


# --------------------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------------------
#
# Unlike the licence policy, a missing table here is not an error. There is no
# allow-list to get wrong: every check still runs, and the defaults ignore only the
# packages pip and venv install for themselves.


class Policy(BaseModel):
    model_config = ConfigDict(frozen=True)

    ignore: frozenset[str] = DEFAULT_IGNORE
    pins: tuple[dict[str, Any], ...] = ()
    new_package_days: int = 90
    source: str = "defaults"
    concurrency: int = 4
    """How many near-miss pairs are adjudicated at once. One request each
    over a shared prefix, as in the other two gates; 1 on a backend that
    serialises."""

    def pin_for(self, package: str) -> dict[str, Any] | None:
        low = normalize(package)
        for pin in self.pins:
            for pattern in pin.get("packages", ()):
                if fnmatch.fnmatch(low, normalize(str(pattern))):
                    return pin
        return None

    def accepts(self, package: str, kind: str) -> bool:
        """Whether a recorded pin covers this finding for this package.

        A pin is scoped to the finding kinds it lists. Accepting ``orphan`` for a
        package does not accept ``squatted`` for it later — which is the whole
        difference between a pin and an ignore.
        """
        if normalize(package) in self.ignore:
            return True
        pin = self.pin_for(package)
        if pin is None:
            return False
        accepted = {str(a).lower() for a in pin.get("accept", ())}
        return kind.lower() in accepted or "*" in accepted


def policy_from_data(data: dict[str, Any], where: str) -> Policy:
    ignore = {normalize(str(n)) for n in data.get("ignore", ())}
    days = int(data.get("new_package_days", 90))
    if days < 0:
        raise ValueError(f"{where}: new_package_days must not be negative")
    # Same rule as the licence gate's, and now the same code: a pin missing
    # its reason and its date is an ignore, and one with a misspelled key is
    # a pin that reads as a decision and accepts nothing.
    check_pins(data.get("pin", ()) or (), subject="packages", where=where)
    return Policy(
        ignore=frozenset(DEFAULT_IGNORE | ignore),
        pins=tuple(data.get("pin", ())),
        new_package_days=days,
        source=where,
        concurrency=int(data.get("concurrency", 4)),
    )


def load_policy(path: Path | None = None, root: Path | None = None) -> Policy:
    """Resolve the provenance policy for ``root``.

    ``path``, then a ``[packages]`` table in the project's ``.vibe-sentinel.toml``,
    then ``security/package-policy.toml``. Falling through all three yields the
    defaults rather than an error: with no allow-list to state, "no policy" means
    "check everything and pin nothing", which is the safe reading.
    """
    root = root or Path.cwd()

    if path is not None:
        if not path.exists():
            raise FileNotFoundError(
                f"No package policy at {path}. Remove --policy to use the "
                f"[packages] table in {root / PROJECT_CONFIG}, or the defaults."
            )
        return policy_from_data(tomllib.loads(path.read_text()), str(path))

    project = root / PROJECT_CONFIG
    if project.is_file():
        data = tomllib.loads(project.read_text())
        if "packages" in data:
            return policy_from_data(data["packages"], f"{project} [packages]")

    standalone = root / POLICY_PATH
    if standalone.is_file():
        return policy_from_data(tomllib.loads(standalone.read_text()), str(standalone))

    return Policy()


# --------------------------------------------------------------------------------------
# The audit
# --------------------------------------------------------------------------------------


class Finding(BaseModel):
    """One provenance fact that needs a human decision."""

    model_config = ConfigDict(frozen=True)

    kind: str
    name: str
    detail: str
    remediation: str
    evidence: tuple[str, ...] = ()


class Inventory(BaseModel):
    """Everything the audit measured, kept so callers can record it as-is."""

    model_config = ConfigDict(frozen=True)

    environment: Environment
    declared: tuple[Requirement, ...]
    installed: dict[str, Installed]
    closure: frozenset[str]
    imports: tuple[SourceImport, ...]
    first_party: frozenset[str]
    project: str

    @property
    def direct(self) -> frozenset[str]:
        return frozenset(r.name for r in self.declared)

    @property
    def external_imports(self) -> tuple[SourceImport, ...]:
        """Imports that are neither stdlib nor this project's own modules."""
        return tuple(
            imp
            for imp in self.imports
            if imp.module not in sys.stdlib_module_names
            and imp.module not in self.first_party
        )


def take_inventory(root: Path) -> Inventory:
    """Read the environment and the project without judging either."""
    installed = installed_distributions()
    declared = declared_requirements(root)
    return Inventory(
        environment=environment(),
        declared=tuple(declared),
        installed=installed,
        closure=requirement_closure({r.name for r in declared}, installed),
        imports=tuple(source_imports(root)),
        first_party=first_party(root),
        project=project_name(root),
    )


def required_by(installed: dict[str, Installed]) -> dict[str, frozenset[str]]:
    """Reverse of ``Requires-Dist``: name -> the installed packages needing it."""
    out: dict[str, set[str]] = {}
    for dist in installed.values():
        for need in dist.requires:
            out.setdefault(need, set()).add(dist.name)
    return {name: frozenset(users) for name, users in out.items()}


def outside_closure(inventory: Inventory) -> frozenset[str]:
    """Installed packages nothing declares and nothing installed requires.

    The roots of the undeclared part of the environment. A package required by
    another orphan is not a root: uninstalling its parent removes it too, so
    reporting it separately would be one finding per node of a tree whose only
    actionable member is the top.
    """
    users = required_by(inventory.installed)
    return frozenset(
        name
        for name in inventory.installed
        if name != inventory.project
        and name not in inventory.closure
        and not users.get(name)
    )


def dragged_in(inventory: Inventory, root: str) -> frozenset[str]:
    """Packages present only because ``root`` requires them, directly or not."""
    found: set[str] = set()
    stack = list(inventory.installed[root].requires)
    while stack:
        current = stack.pop()
        if current in found or current in inventory.closure:
            continue
        if current not in inventory.installed:
            continue
        found.add(current)
        stack.extend(inventory.installed[current].requires)
    return frozenset(found)


def audit(inventory: Inventory, policy: Policy) -> list[Finding]:
    """Every offline provenance finding. No network, no model, no judgement calls.

    Checks are ordered so that a name is reported once, under the most serious
    heading it qualifies for: an import that resolves to nothing is a phantom, not
    also an undeclared dependency.
    """
    findings: list[Finding] = []
    owners = import_owners()
    direct = inventory.direct

    # --- imports that resolve to nothing -----------------------------------------
    for imp in inventory.external_imports:
        if imp.module in owners or is_importable(imp.module):
            continue
        if policy.accepts(imp.module, "phantom"):
            continue
        findings.append(
            Finding(
                kind="phantom",
                name=imp.module,
                detail=(
                    f"imported in {len(imp.sites)} place(s); no installed "
                    f"distribution provides it, it is not in the standard library, "
                    f"and it is not a module of this project"
                ),
                remediation=(
                    "Check the name exists before installing it — a model that does "
                    "not know a library invents one, and the import reads exactly "
                    "like a real one. Confirm on PyPI (or run with --online), then "
                    "either declare it in pyproject.toml or delete the import."
                ),
                evidence=imp.sites,
            )
        )

    # --- imports satisfied by something nobody declared ---------------------------
    phantom_names = {f.name for f in findings}
    for imp in inventory.external_imports:
        if imp.module in phantom_names:
            continue
        providers = owners.get(imp.module, ())
        undeclared = [
            p for p in providers if p not in direct and p != inventory.project
        ]
        if not undeclared or any(policy.accepts(p, "undeclared") for p in undeclared):
            continue
        name = undeclared[0]
        transitive = name in inventory.closure
        findings.append(
            Finding(
                kind="undeclared",
                name=name,
                detail=(
                    f"imported as {imp.module!r} but not declared in pyproject.toml"
                    + (
                        "; it is here only because another dependency happens to "
                        "require it, so a change upstream removes it without warning"
                        if transitive
                        else "; nothing in the project asks for it at all"
                    )
                ),
                remediation=(
                    f"Add {name} to [project.dependencies] (or the group that needs "
                    f"it), or stop importing it."
                ),
                evidence=imp.sites,
            )
        )

    # --- installed, undeclared, and required by nothing ---------------------------
    #
    # Only ROOTS are reported. An orphan's own dependencies are outside the closure
    # too, so reporting all of them turns one stray `pip install openai` into five
    # findings, four of which nobody can act on. The root is named and what it drags
    # in is listed beside it, because uninstalling the root is the whole fix.
    for name in sorted(outside_closure(inventory)):
        if policy.accepts(name, "orphan"):
            continue
        dist = inventory.installed[name]
        dragged = sorted(dragged_in(inventory, name))
        findings.append(
            Finding(
                kind="orphan",
                name=name,
                detail=(
                    f"{name} {dist.version} is installed, nothing declares it, and "
                    f"no installed package requires it"
                    + (f"; it pulls in {', '.join(dragged)}" if dragged else "")
                ),
                remediation=(
                    f"Something put it here and nothing recorded why — the usual "
                    f"cause is an agent running pip install and moving on. Declare "
                    f"it if it is wanted, otherwise remove it: pip uninstall {name}"
                ),
                evidence=((f"installer: {dist.installer}",) if dist.installer else ())
                + tuple(f"drags in {d}" for d in dragged),
            )
        )

    # --- declared but absent from the environment being measured ------------------
    #
    # The guard on everything above. Every other check compares a manifest against
    # an environment, and a manifest whose own unconditional dependencies are not
    # installed is not describing this environment at all — so the orphan list is
    # measuring the wrong thing and should say so rather than be believed.
    #
    # Only unconditional project dependencies count: an extra nobody asked for and
    # a requirement behind a platform marker are both legitimately absent.
    for req in inventory.declared:
        if req.group != "project" or req.marker:
            continue
        if req.name in inventory.installed or policy.accepts(req.name, "uninstalled"):
            continue
        findings.append(
            Finding(
                kind="uninstalled",
                name=req.name,
                detail=(
                    f"declared as {req.raw!r} but not installed in "
                    f"{inventory.environment.label} — this environment is not the "
                    f"one the project describes, so every other finding here is "
                    f"measuring the wrong environment"
                ),
                remediation=(
                    "Install the project into the environment you are checking "
                    "(pip install -e '.[dev]'), or re-run with the interpreter that "
                    "already has it."
                ),
                evidence=(f"environment: {inventory.environment.prefix}",),
            )
        )

    # --- declared with no version bound ------------------------------------------
    for req in inventory.declared:
        if req.specifier or policy.accepts(req.name, "unconstrained"):
            continue
        findings.append(
            Finding(
                kind="unconstrained",
                name=req.name,
                detail=(
                    f"declared as {req.raw!r} in {req.group} with no version bound, "
                    f"so any future release satisfies it — including one published "
                    f"from a compromised account"
                ),
                remediation=(
                    f"Give it a floor, and an upper bound if the API is not stable: "
                    f"{req.name}>=<the version you tested>"
                ),
            )
        )

    # --- two installed names one edit apart --------------------------------------
    for first, second in near_misses(sorted(inventory.installed)):
        # The suspicious member is the one nothing asked for.
        outside = [n for n in (first, second) if n not in inventory.closure]
        name = outside[0] if len(outside) == 1 else first
        if policy.accepts(name, "near-miss"):
            continue
        findings.append(
            Finding(
                kind="near-miss",
                name=name,
                detail=(
                    f"{first!r} and {second!r} are one edit apart and both installed"
                ),
                remediation=(
                    "Confirm both are intended. One character between two installed "
                    "names is the typosquat shape, and it survives review because "
                    "the wrong one reads correctly."
                ),
                evidence=(first, second),
            )
        )

    return findings


# --------------------------------------------------------------------------------------
# Online confirmation
# --------------------------------------------------------------------------------------
#
# Opt-in, and narrow on purpose. It asks PyPI only about names that are ALREADY
# suspect plus the direct dependencies pyproject.toml already states in public — never
# the whole installed set, which would hand a third party an inventory of the machine.


class RegistryFact(BaseModel):
    """What the index says about one name."""

    model_config = ConfigDict(frozen=True)

    name: str
    exists: bool = False
    first_release: str = ""
    release_count: int = 0
    age_days: int = -1
    error: str = ""


def _earliest_upload(payload: dict[str, Any]) -> str:
    """Earliest upload timestamp across every release, as an ISO date."""
    stamps: list[str] = []
    for files in (payload.get("releases") or {}).values():
        for entry in files or []:
            stamp = entry.get("upload_time_iso_8601") or entry.get("upload_time")
            if stamp:
                stamps.append(str(stamp))
    if not stamps:
        for entry in payload.get("urls") or []:
            stamp = entry.get("upload_time_iso_8601") or entry.get("upload_time")
            if stamp:
                stamps.append(str(stamp))
    return min(stamps) if stamps else ""


def _age_days(stamp: str) -> int:
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return -1
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (datetime.now(UTC) - parsed).days


def registry_facts(
    names: list[str], timeout: float = 10.0, client: httpx.Client | None = None
) -> dict[str, RegistryFact]:
    """Ask PyPI about each name. Failures are recorded, never treated as absence.

    A 404 means the index has no such project; a timeout means nothing at all was
    learned, and the two must not collapse into one answer — "we could not check"
    reported as "it is not there" is the shape of a review that did not happen.
    """
    owned = client is None
    session = client or httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "vibe-sentinel (dependency provenance check)"},
    )
    facts: dict[str, RegistryFact] = {}
    try:
        for name in sorted(set(names)):
            try:
                response = session.get(PYPI_JSON.format(name=name))
            except httpx.HTTPError as e:
                facts[name] = RegistryFact(name=name, error=f"{type(e).__name__}: {e}")
                continue
            if response.status_code == 404:
                facts[name] = RegistryFact(name=name, exists=False)
                continue
            if response.status_code != 200:
                facts[name] = RegistryFact(
                    name=name, error=f"HTTP {response.status_code}"
                )
                continue
            try:
                payload = response.json()
            except ValueError as e:
                facts[name] = RegistryFact(name=name, error=f"unreadable JSON: {e}")
                continue
            first = _earliest_upload(payload)
            facts[name] = RegistryFact(
                name=name,
                exists=True,
                first_release=first,
                release_count=len(payload.get("releases") or {}),
                age_days=_age_days(first) if first else -1,
            )
    finally:
        if owned:
            session.close()
    return facts


def names_to_confirm(inventory: Inventory, findings: list[Finding]) -> list[str]:
    """Which names the online step may ask about.

    Two groups, both defensible: names this audit has already flagged, and the
    direct dependencies pyproject.toml states in public anyway.
    """
    return sorted(
        {f.name for f in findings if f.kind != "unconstrained"} | set(inventory.direct)
    )


def confirm_online(
    inventory: Inventory,
    findings: list[Finding],
    facts: dict[str, RegistryFact],
    policy: Policy,
) -> list[Finding]:
    """Fold registry answers into the offline findings.

    A phantom becomes ``unregistered`` when the index has never heard of the name,
    and ``squatted`` when it has heard of it very recently — a name your code
    invented that someone registered days ago is the attack, not a coincidence.
    """
    out: list[Finding] = []
    seen_error = False

    for finding in findings:
        fact = facts.get(finding.name)
        if finding.kind != "phantom" or fact is None:
            out.append(finding)
            continue
        if fact.error:
            seen_error = True
            out.append(finding)
            continue
        if not fact.exists:
            out.append(
                Finding(
                    kind="unregistered",
                    name=finding.name,
                    detail=(
                        f"imported in {len(finding.evidence)} place(s) and PyPI has "
                        f"no project by that name — the name does not exist"
                    ),
                    remediation=(
                        "Delete the import. Then look for what the code was reaching "
                        "for: a name that resolves to nothing is a name something "
                        "invented, and the surrounding code was written against a "
                        "library that was invented with it."
                    ),
                    evidence=finding.evidence,
                )
            )
            continue
        recent = 0 <= fact.age_days <= policy.new_package_days
        out.append(
            Finding(
                kind="squatted" if recent else "phantom",
                name=finding.name,
                detail=(
                    f"imported but not installed; PyPI DOES have a project called "
                    f"{finding.name!r}, first published {fact.first_release[:10]} "
                    f"({fact.age_days} days ago, {fact.release_count} release(s))"
                    if recent
                    else f"imported but not installed; a PyPI project of this name "
                    f"exists, first published {fact.first_release[:10]}"
                ),
                remediation=(
                    "Do not install it to find out. A name your code imports without "
                    "anyone installing it, which appeared on the index this recently, "
                    "is the slopsquatting shape: the name was invented first and "
                    "registered afterwards. Read the project page and its source "
                    "before anything runs it."
                    if recent
                    else f"Declare and install it if it is genuinely wanted: add "
                    f"{finding.name} to pyproject.toml."
                ),
                evidence=finding.evidence,
            )
        )

    # --- dependencies that are new arrivals on the index --------------------------
    for name in sorted(inventory.direct):
        fact = facts.get(name)
        if fact is None or fact.error or not fact.exists:
            seen_error = seen_error or bool(fact and fact.error)
            continue
        if not (0 <= fact.age_days <= policy.new_package_days):
            continue
        if policy.accepts(name, "newborn"):
            continue
        out.append(
            Finding(
                kind="newborn",
                name=name,
                detail=(
                    f"declared dependency; first published {fact.first_release[:10]}, "
                    f"{fact.age_days} days ago, {fact.release_count} release(s)"
                ),
                remediation=(
                    "New is not wrong, but it is the state every squatted package is "
                    "in. Confirm the project is what it claims — repository, author, "
                    "release history — then record the decision as a [[packages.pin]] "
                    "accepting 'newborn'."
                ),
            )
        )

    if seen_error:
        failed = sorted(n for n, f in facts.items() if f.error)
        out.append(
            Finding(
                kind="unchecked",
                name=", ".join(failed[:5]) + (" ..." if len(failed) > 5 else ""),
                detail=(
                    f"{len(failed)} name(s) could not be confirmed against the index; "
                    f"the offline result stands but the online half did not run"
                ),
                remediation=(
                    "Re-run with network access. A name reported as merely 'phantom' "
                    "here has not been checked against the registry, and absence of "
                    "an answer is not an answer."
                ),
                evidence=tuple(f"{n}: {facts[n].error}" for n in failed[:10]),
            )
        )
    return out


# --------------------------------------------------------------------------------------
# Adjudicating the one finding that is a judgement
# --------------------------------------------------------------------------------------
#
# Every other kind here is a fact. An import resolves or it does not; a version is
# bounded or it is not; the index has heard of a name or it has not. ``near-miss`` is
# not that shape. "Two installed names one edit apart" describes a typosquat and a
# rewrite shipped under a new name identically, and the rule already carries one
# exception written by hand -- names differing only in digits -- discovered because
# ``httpx``/``httpx2`` and ``httpcore``/``httpcore2`` are all installed in this
# repository's own environment. A rule accumulating exceptions because the question is
# a judgement is the signal to stop writing exceptions.
#
# So the shape is the credentials gate's, exactly: the mechanism finds the candidate,
# the model adjudicates it, and it can only ever settle one -- ``near_misses`` decides
# what is asked about, and nothing the model says adds a pair to that list.
#
# Two things are deliberately NOT the credentials gate's:
#
#   - A pair nobody reviewed still fails. There, an unadjudicated pattern hit on a
#     string is weak evidence and ``unreviewed`` does not fail; here the finding
#     failed on its own before any model existed, and a gate that gets weaker when
#     the GPU is off is the wrong trade. ``--no-model`` leaves this gate's verdict
#     byte-identical to what it was.
#   - The finding's key never moves. The model names which of the two it thinks is
#     the imposter and that goes in the record beside the finding, never into
#     ``Finding.name`` -- a key that shifted with an opinion would not be stable
#     across runs, which is the one thing a gate finding's key has to be.


NEAR_MISS_SYSTEM_PROMPT = """\
You are looking at two Python packages installed in the same environment
whose names are one character apart.

That shape has two ordinary explanations and one bad one:

- Two unrelated real packages. Short names collide; this is common.
- A project shipping a rewrite under a new name -- a trailing number, a
  version suffix. Both are real, both are meant to be there.
- A typosquat: one name is a misspelling of the other, registered by
  somebody counting on the misspelling, and it is installed because a
  human or a model typed it wrong.

Decide which. The evidence below is everything this machine knows about
the two without asking anybody: what each says it does, what version each
is at, what installed it, and -- the useful part -- whether anything
actually asked for it. A package nothing declares and nothing else
requires is in the environment for no recorded reason.

Verdicts:
- distinct:   both are real packages that happen to have close names, or
              one is a rewrite the same project shipped under a new name.
- typosquat:  one name reads as a misspelling of the other. Put that name
              in "suspect".
- unclear:    the evidence does not settle it. A real answer, not a hedge
              -- it is what sends this pair to a person.

Do not guess from the names alone when the descriptions disagree: two
packages that do different things are two packages. And a package that
nothing declares, nothing requires, and that ships no description at all
is the shape worth saying "typosquat" about.
"""


class NearMissJudgement(BaseModel):
    """One near-miss pair and what was decided about it."""

    model_config = ConfigDict(frozen=True)

    finding: Finding
    verdict: str = "unreviewed"
    """``distinct``, ``typosquat``, ``unclear``, ``pinned`` or ``unreviewed``.

    The last two are not the model's: ``pinned`` means the policy already
    accepted this name, and ``unreviewed`` means nobody looked. Only
    ``distinct`` clears the finding."""
    suspect: str = ""
    reason: str = ""
    reviewed: bool = False
    """Whether the model actually answered about this pair."""

    @property
    def failing(self) -> bool:
        """Everything but a pin and an affirmative ``distinct``.

        A pair nobody reviewed fails exactly as it did before this step
        existed. The model is here to settle a finding, never to be the
        reason one goes unreported."""
        return self.verdict not in ("distinct", "pinned")


class Adjudication(BaseModel):
    """A provenance audit, with its near-misses settled or standing."""

    model_config = ConfigDict(frozen=True)

    findings: tuple[Finding, ...] = ()
    """Every finding the audit made, near-misses included. Nothing is
    dropped here — :attr:`judgements` says which of them still stand."""
    judgements: tuple[NearMissJudgement, ...] = ()
    reviewed: bool = False
    """True only when the model settled at least one pair. False means every
    near-miss below is mechanical and no rendering may claim otherwise."""
    note: str = ""
    """Why nothing was adjudicated, when nothing was."""

    def failing(self) -> tuple[Finding, ...]:
        """The findings that fail the gate."""
        cleared = {j.finding.name for j in self.judgements if not j.failing}
        return tuple(
            f
            for f in self.findings
            if not (f.kind == "near-miss" and f.name in cleared)
        )


def build_near_miss_context(inventory: Inventory, findings: list[Finding]) -> str:
    """The shared half of the prompt: every pair, and what is known of them.

    This is the **system** message and it is byte-identical for every pair
    asked about in one run, so a server that caches prefixes prefills the
    environment once however many pairs there are. The divergent tail is
    one pair's question, from :func:`build_near_miss_question`.
    """
    users = required_by(inventory.installed)
    imported = {i.module for i in inventory.imports}
    lines = [
        NEAR_MISS_SYSTEM_PROMPT,
        "",
        f"Environment: {inventory.environment.label}",
        "",
    ]

    seen: set[str] = set()
    for finding in findings:
        for name in finding.evidence[:2]:
            if name in seen:
                continue
            seen.add(name)
            dist = inventory.installed.get(name)
            if dist is None:
                lines.append(f"  {name}: not installed")
                continue
            asked = (
                "declared in this project"
                if name in inventory.direct
                else (
                    f"required by {', '.join(sorted(users[name])[:4])}"
                    if users.get(name)
                    else "NOTHING declares it and NOTHING requires it"
                )
            )
            lines += [
                f"  {name} {dist.version}",
                f"    says it is:  {dist.summary or '(no description in its metadata)'}",
                f"    installed by: {dist.installer or 'unrecorded'}",
                f"    asked for by: {asked}",
                f"    imported in this project's source: "
                f"{'yes' if name in imported else 'no'}",
                "",
            ]
    return "\n".join(lines)


def build_near_miss_question(finding: Finding) -> str:
    """The divergent half: one pair, named."""
    from vibe_sentinel.schemas import BREVITY

    pair = " and ".join(repr(n) for n in finding.evidence[:2])
    return (
        f"Answer about this one pair, and nothing else.\n\n"
        f"{pair} are both installed and are one edit apart. Are these two "
        f"real packages, or is one of them a misspelling of the other?\n\n"
        f"{BREVITY}"
    )


async def review_near_misses(
    findings: list[Finding],
    inventory: Inventory,
    policy: Policy,
    config: SentinelConfig | None = None,
) -> list[NearMissJudgement]:
    """Ask about each pair. One request each, over one shared context.

    Imported lazily and awaited rather than run: this reaches httpx, which
    the offline path must not pay for, and it is called from the scan,
    which is already inside a loop.
    """
    import asyncio

    from loguru import logger

    from vibe_sentinel.config import SentinelConfig
    from vibe_sentinel.exceptions import LLMConnectionError
    from vibe_sentinel.json_schema import clip_to_bounds
    from vibe_sentinel.llm import llm_query
    from vibe_sentinel.schemas import _NEAR_MISS_SCHEMA, NearMissOpinion

    if not findings:
        return []

    config = config or SentinelConfig()
    # Deterministic and short: this is a three-way choice with a sentence
    # of reason, the same tuning the other two gates use.
    tuned = config.model_copy(update={"temperature": 0.0, "max_tokens": 512})
    context = build_near_miss_context(inventory, findings)

    async def ask_all() -> list[Any]:
        limit = asyncio.Semaphore(max(1, policy.concurrency))

        async def one(finding: Finding) -> tuple[Finding, dict[str, Any] | None]:
            async with limit:
                return finding, await llm_query(
                    context,
                    build_near_miss_question(finding),
                    _NEAR_MISS_SCHEMA,
                    f"near-miss-{finding.name}",
                    config=tuned,
                )

        return await asyncio.gather(*(one(f) for f in findings), return_exceptions=True)

    try:
        results = await ask_all()
    except LLMConnectionError as e:
        logger.error("packages: model unreachable ({}) — nothing adjudicated", e)
        return [NearMissJudgement(finding=f, reason=str(e)) for f in findings]

    judgements: list[NearMissJudgement] = []
    for item in results:
        if isinstance(item, BaseException) or not isinstance(item, tuple):
            logger.warning("packages: a near-miss review failed ({})", item)
            continue
        finding, raw = item
        if raw is None:
            judgements.append(
                NearMissJudgement(finding=finding, reason="the model did not answer")
            )
            continue
        try:
            opinion = NearMissOpinion.model_validate(
                clip_to_bounds(NearMissOpinion, raw)
            )
        except Exception as e:  # noqa: BLE001 - one bad answer must not lose the rest
            logger.warning("packages: unusable verdict for {} ({})", finding.name, e)
            judgements.append(
                NearMissJudgement(finding=finding, reason=f"unusable answer: {e}")
            )
            continue
        judgements.append(
            NearMissJudgement(
                finding=finding,
                verdict=opinion.verdict,
                suspect=opinion.suspect,
                reason=opinion.reason,
                reviewed=True,
            )
        )
    return judgements


async def adjudicate(
    inventory: Inventory,
    findings: list[Finding],
    policy: Policy,
    config: SentinelConfig | None = None,
    *,
    use_model: bool = True,
) -> Adjudication:
    """Settle every near-miss: by pin, or by the model, or not at all.

    A pin first, because it is a decision somebody already recorded and
    an 8B model does not get a vote on it. Everything that is not a
    near-miss passes through untouched — those are facts, and a fact is
    not improved by an opinion about it.
    """
    pairs = [f for f in findings if f.kind == "near-miss"]
    others = tuple(findings)

    settled = [
        NearMissJudgement(
            finding=f,
            verdict="pinned",
            reason=str((policy.pin_for(f.name) or {}).get("reason", "")).strip(),
        )
        for f in pairs
        if policy.accepts(f.name, "near-miss")
    ]
    to_ask = [f for f in pairs if not policy.accepts(f.name, "near-miss")]

    if not to_ask:
        return Adjudication(findings=others, judgements=tuple(settled))

    if not use_model:
        return Adjudication(
            findings=others,
            judgements=tuple(settled + [NearMissJudgement(finding=f) for f in to_ask]),
            note="--no-model: these pairs stand, adjudicated by nobody",
        )

    judged = await review_near_misses(to_ask, inventory, policy, config)
    return Adjudication(
        findings=others,
        judgements=tuple(settled + judged),
        reviewed=any(j.reviewed for j in judged),
    )


def by_severity(findings: list[Finding]) -> list[Finding]:
    """Findings most serious first, then by name.

    Alphabetical order would print ``newborn`` above ``squatted``, which buries
    the one finding that means someone registered a name the code invented.
    """
    return sorted(findings, key=lambda f: (severity_rank(f.kind), f.name))
